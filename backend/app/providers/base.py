"""Shared async provider client: HTTP + SDK transport with production resilience.

Every external DashScope call — compatible-mode chat/VL over HTTP, and the
``dashscope`` SDK for image/TTS/video — funnels through :class:`ProviderClient`,
which layers:

* per-call timeouts,
* exponential-backoff-with-jitter retries on transient failures (tenacity),
* a lightweight circuit breaker (open → half-open probe → closed),
* a token-bucket rate limiter,
* structured per-call logging (model, latency, status — never the key/prompt),
* a cost-accounting hook: each call records a :class:`~app.providers.types.Usage`
  into an injectable sink (default: log + in-memory accumulator) that the budget
  service later subscribes to.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

import anyio
import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

from .errors import (
    AuthenticationError,
    CircuitOpenError,
    ModelNotAvailable,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    ResponseParseError,
    TransientProviderError,
)
from .types import Usage, UsageTotals

logger = get_logger("app.providers")

T = TypeVar("T")

#: Type of a cost sink: receives every :class:`Usage` the client records.
UsageSink = Callable[[Usage], None]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResilienceConfig:
    """Tunables for retries, the circuit breaker, the rate limiter, timeouts."""

    max_attempts: int = 4
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    backoff_jitter_s: float = 0.3
    breaker_failure_threshold: int = 5
    breaker_recovery_s: float = 20.0
    rate_per_s: float = 8.0
    rate_burst: int = 8
    default_timeout_s: float = 60.0


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


class TokenBucket:
    """An async token-bucket rate limiter (refills continuously at ``rate``)."""

    def __init__(self, rate_per_s: float, burst: int) -> None:
        self._rate = max(rate_per_s, 0.001)
        self._capacity = float(max(burst, 1))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while True:
            async with self._lock:
                now = time.monotonic()
                refilled = self._tokens + (now - self._updated) * self._rate
                self._tokens = min(self._capacity, refilled)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_s = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait_s)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips open after N consecutive failures; probes once after a cool-down."""

    def __init__(self, failure_threshold: int, recovery_s: float) -> None:
        self._threshold = max(failure_threshold, 1)
        self._recovery_s = recovery_s
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    async def before_call(self) -> None:
        """Raise :class:`CircuitOpenError` if the breaker is open and cooling."""
        async with self._lock:
            if self._state is BreakerState.OPEN:
                if time.monotonic() - self._opened_at >= self._recovery_s:
                    self._state = BreakerState.HALF_OPEN
                else:
                    raise CircuitOpenError(
                        "circuit breaker open; rejecting call without attempting",
                    )

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._state = BreakerState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            tripped = self._consecutive_failures >= self._threshold
            if self._state is BreakerState.HALF_OPEN or tripped:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()


# --------------------------------------------------------------------------- #
# Default cost sink
# --------------------------------------------------------------------------- #


class LoggingUsageSink:
    """Default cost sink: logs each :class:`Usage` and accumulates totals."""

    def __init__(self) -> None:
        self.totals = UsageTotals()

    def __call__(self, usage: Usage) -> None:
        self.totals.add(usage)
        logger.info("provider.usage", **usage.as_log_fields())


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def data_uri(raw: bytes, mime: str) -> str:
    """Encode raw bytes as a ``data:`` URI for inline image/audio inputs."""
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def sdk_get(obj: Any, key: str) -> Any:
    """Read ``key`` from a ``dashscope`` response node (item *or* attr access)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return getattr(obj, key, None)


def classify_status(
    status: int,
    *,
    code: str | None = None,
    message: str | None = None,
    request_id: str | None = None,
) -> ProviderError:
    """Map a DashScope HTTP status / error code to a typed exception."""
    text = (message or "").lower()
    detail = message or code or f"HTTP {status}"
    if status == 429 or (code and "throttl" in code.lower()):
        return RateLimited(detail, code=code, status_code=status, request_id=request_id)
    if status in (401, 403):
        return AuthenticationError(detail, code=code, status_code=status, request_id=request_id)
    if status >= 500:
        return TransientProviderError(detail, code=code, status_code=status, request_id=request_id)
    if "model not exist" in text or "model not found" in text or (code == "ModelNotFound"):
        return ModelNotAvailable(detail, code=code, status_code=status, request_id=request_id)
    return ProviderBadRequest(detail, code=code, status_code=status, request_id=request_id)


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class ProviderClient:
    """Resilient transport shared by all DashScope providers."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        usage_sink: UsageSink | None = None,
        resilience: ResilienceConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = resilience or ResilienceConfig()
        self._default_sink = LoggingUsageSink()
        self.usage_sink: UsageSink = usage_sink or self._default_sink
        self._rate = TokenBucket(self.config.rate_per_s, self.config.rate_burst)
        self._breaker = CircuitBreaker(
            self.config.breaker_failure_threshold,
            self.config.breaker_recovery_s,
        )
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self.config.default_timeout_s),
        )
        self._dashscope_configured = False

    # -- URLs ------------------------------------------------------------- #

    @property
    def base_url(self) -> str:
        return self.settings.dashscope_base_url.rstrip("/")

    @property
    def compat_base(self) -> str:
        """OpenAI-compatible base (chat + VL + model list)."""
        return f"{self.base_url}/compatible-mode/v1"

    @property
    def native_base(self) -> str:
        """Native DashScope base (image/TTS/video async services)."""
        return f"{self.base_url}/api/v1"

    @property
    def ws_url(self) -> str:
        host = self.base_url.split("://", 1)[-1]
        return f"wss://{host}/api-ws/v1/inference"

    # -- Cost accounting -------------------------------------------------- #

    @property
    def usage_totals(self) -> UsageTotals | None:
        """In-memory totals when the default sink is in use (else ``None``)."""
        sink = self.usage_sink
        if isinstance(sink, LoggingUsageSink):
            return sink.totals
        return None

    def record_usage(self, usage: Usage) -> None:
        """Push a :class:`Usage` event to the configured sink."""
        try:
            self.usage_sink(usage)
        except Exception:  # noqa: BLE001 - a broken sink must never fail a call
            logger.warning("provider.usage_sink_error", model=usage.model, exc_info=True)

    # -- DashScope SDK globals -------------------------------------------- #

    def configure_dashscope(self) -> None:
        """Point the ``dashscope`` SDK globals at the configured intl endpoint.

        Idempotent; the SDK reads these module-level globals, but we also pass
        ``api_key`` explicitly on every call as defence in depth.
        """
        if self._dashscope_configured:
            return
        import dashscope

        dashscope.api_key = self.settings.dashscope_api_key
        dashscope.base_http_api_url = self.native_base
        dashscope.base_websocket_api_url = self.ws_url
        self._dashscope_configured = True

    @property
    def api_key(self) -> str:
        return self.settings.dashscope_api_key

    # -- Core resilient executor ------------------------------------------ #

    async def _execute(self, attempt: Callable[[], Awaitable[T]], *, op: str, model: str) -> T:
        """Run ``attempt`` under rate-limit + breaker + retry, logging the call."""
        await self._breaker.before_call()
        started = time.perf_counter()
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.config.max_attempts),
            wait=wait_exponential_jitter(
                initial=self.config.backoff_base_s,
                max=self.config.backoff_max_s,
                jitter=self.config.backoff_jitter_s,
            ),
            retry=retry_if_exception_type(TransientProviderError),
            reraise=True,
        )
        attempt_no = 0
        try:
            async for tenacity_attempt in retrying:
                attempt_no += 1
                with tenacity_attempt:
                    await self._rate.acquire()
                    try:
                        result = await attempt()
                    except TransientProviderError as exc:
                        await self._breaker.record_failure()
                        logger.warning(
                            "provider.call_retryable_error",
                            op=op,
                            model=model,
                            attempt=attempt_no,
                            error=type(exc).__name__,
                            status=exc.status_code,
                            code=exc.code,
                        )
                        raise
                    except ProviderError:
                        # Non-retryable (4xx/auth): caller's problem, not a fault
                        # the breaker should count. Surface immediately.
                        raise
                    await self._breaker.record_success()
                    logger.info(
                        "provider.call_ok",
                        op=op,
                        model=model,
                        attempt=attempt_no,
                        latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    )
                    return result
        except ProviderError as exc:
            logger.warning(
                "provider.call_failed",
                op=op,
                model=model,
                attempts=attempt_no,
                error=type(exc).__name__,
                status=exc.status_code,
                code=exc.code,
            )
            raise
        raise ProviderError("retry loop exited without a result")  # pragma: no cover

    # -- HTTP (compatible-mode + native) ---------------------------------- #

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if extra:
            headers.update(extra)
        return headers

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        op: str,
        model: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Resilient JSON request; raises typed errors on non-2xx / bad bodies."""
        call_timeout = timeout or self.config.default_timeout_s

        async def attempt() -> dict[str, Any]:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    json=json,
                    headers=self._auth_headers(headers),
                    timeout=call_timeout,
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(f"request to {op} timed out") from exc
            except httpx.HTTPError as exc:
                raise TransientProviderError(f"transport error calling {op}: {exc}") from exc
            return self._parse_http_json(resp)

        return await self._execute(attempt, op=op, model=model)

    def _parse_http_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            if resp.is_success:
                raise ResponseParseError("response was not valid JSON") from exc
            raise classify_status(resp.status_code, message=resp.text[:200]) from exc
        if resp.is_success:
            return body
        # Error body: support both compat-mode ({"error": {...}}) and native shapes.
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            raise classify_status(
                resp.status_code,
                code=err.get("code") or err.get("type"),
                message=err.get("message"),
                request_id=body.get("request_id") if isinstance(body, dict) else None,
            )
        raise classify_status(
            resp.status_code,
            code=body.get("code") if isinstance(body, dict) else None,
            message=body.get("message") if isinstance(body, dict) else None,
            request_id=body.get("request_id") if isinstance(body, dict) else None,
        )

    async def download(
        self,
        url: str,
        *,
        op: str = "download",
        timeout: float | None = None,
    ) -> bytes:
        """Fetch raw bytes (image/clip/audio result) with the same resilience."""
        call_timeout = timeout or self.config.default_timeout_s

        async def attempt() -> bytes:
            try:
                resp = await self._http.get(url, timeout=call_timeout)
            except httpx.TimeoutException as exc:
                raise ProviderTimeout("asset download timed out") from exc
            except httpx.HTTPError as exc:
                raise TransientProviderError(f"asset download transport error: {exc}") from exc
            if not resp.is_success:
                raise classify_status(resp.status_code, message="asset download failed")
            return resp.content

        return await self._execute(attempt, op=op, model="-")

    # -- SDK (image / TTS / video) ---------------------------------------- #

    async def call_sdk(
        self,
        func: Callable[..., T],
        *args: Any,
        op: str,
        model: str,
        timeout: float | None = None,
        check_response: bool = True,
        **kwargs: Any,
    ) -> T:
        """Run a blocking ``dashscope`` SDK callable in a worker thread.

        The result is inspected for a DashScope ``status_code``; non-OK statuses
        become typed exceptions (so transient ones drive retries). SDK-internal
        exceptions are classified too.
        """
        self.configure_dashscope()
        call_timeout = timeout or self.config.default_timeout_s
        bound = functools.partial(func, *args, **kwargs)

        async def attempt() -> T:
            try:
                with anyio.fail_after(call_timeout):
                    result = await anyio.to_thread.run_sync(bound, abandon_on_cancel=True)
            except TimeoutError as exc:
                raise ProviderTimeout(f"{op} SDK call timed out") from exc
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize SDK/network faults
                raise self._classify_sdk_exception(exc) from exc
            if check_response:
                self._raise_for_sdk_response(result)
            return result

        return await self._execute(attempt, op=op, model=model)

    @staticmethod
    def _classify_sdk_exception(exc: Exception) -> ProviderError:
        name = type(exc).__name__
        transient_markers = ("Timeout", "Connection", "Temporarily", "WebSocket", "socket")
        if any(m.lower() in name.lower() for m in transient_markers):
            return TransientProviderError(f"transient SDK error ({name}): {exc}")
        if any(m.lower() in str(exc).lower() for m in ("timed out", "timeout", "connection")):
            return TransientProviderError(f"transient SDK error ({name}): {exc}")
        return ProviderError(f"SDK error ({name}): {exc}")

    @staticmethod
    def _raise_for_sdk_response(result: Any) -> None:
        status = getattr(result, "status_code", None)
        if status is None:
            return
        status_int = int(status)
        if status_int == 200:
            return
        raise classify_status(
            status_int,
            code=getattr(result, "code", None),
            message=getattr(result, "message", None),
            request_id=getattr(result, "request_id", None),
        )

    # -- Lifecycle -------------------------------------------------------- #

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ProviderClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "LoggingUsageSink",
    "ProviderClient",
    "ResilienceConfig",
    "TokenBucket",
    "UsageSink",
    "classify_status",
    "data_uri",
    "sdk_get",
]
