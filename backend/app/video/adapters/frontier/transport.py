"""A thin async httpx client for the frontier adapters, behind a settings flag.

Each frontier provider has its own base URL + bearer key, so rather than reuse the
DashScope-shaped :class:`~app.providers.base.ProviderClient`, this is a focused
transport that:

* refuses to issue *any* real network call unless ``settings.frontier_video_enabled``
  is on (a second, transport-level spend brake on top of the global
  ``KINORA_LIVE_VIDEO`` gate that the adapters enforce in ``submit``);
* accepts an injectable :class:`httpx.AsyncBaseTransport` (tests pass a deterministic
  :class:`httpx.MockTransport`);
* normalises transport faults + non-2xx bodies into the frontier error taxonomy via
  an injectable, per-provider error mapper;
* retries *retryable* faults with bounded exponential backoff + jitter (deterministic
  in tests via a zeroed backoff config and an injectable sleeper).

It is intentionally small: no circuit breaker / rate limiter here (the router owns
health; the provider gateway owns rate-limiting for the DashScope path). The frontier
layer's resilience is retries + the canonical error taxonomy.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import get_logger

from .errors import (
    FrontierBadResponse,
    FrontierError,
    FrontierErrorCode,
    FrontierServerError,
    build_error,
    code_for_status,
)

logger = get_logger("app.video.adapters.frontier.transport")

#: Maps a parsed (status, body) → a canonical FrontierError. Each provider supplies
#: its own (its native code/message live at provider-specific JSON paths).
ErrorMapper = Callable[[int, Any], FrontierError]

#: An async sleeper (injectable so tests advance backoff without real waits).
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FrontierRetryConfig:
    """Bounded retry/backoff for the frontier transport (deterministic in tests)."""

    max_attempts: int = 4
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    backoff_jitter_s: float = 0.3
    request_timeout_s: float = 60.0
    connect_timeout_s: float = 10.0


class FrontierTransportDisabled(FrontierError):  # noqa: N818 - frontier taxonomy naming
    """Raised when a real network call is attempted with the transport flag off.

    Distinct from :class:`~app.providers.errors.LiveVideoDisabled`: that gate is the
    *global* spend brake the adapter checks first; this is the *transport*-level brake
    so even a mis-wired adapter cannot hit the network when frontier video is disabled.
    Non-retryable (flipping it on is a config change, not a retry).
    """

    code_enum = FrontierErrorCode.INVALID_REQUEST
    retryable = False


def _default_error_mapper(status: int, body: Any) -> FrontierError:
    """Fallback mapper: classify by HTTP status, pull a message if the body is JSON."""
    message = f"HTTP {status}"
    native_code: str | None = None
    request_id: str | None = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            native_code = err.get("code") or err.get("type")
        else:
            message = str(body.get("message") or body.get("detail") or message)
            native_code = body.get("code")
        request_id = body.get("id") or body.get("request_id")
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        native_code=str(native_code) if native_code is not None else None,
        status_code=status,
        request_id=str(request_id) if request_id is not None else None,
    )


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class FrontierTransport:
    """Resilient JSON/bytes transport for one frontier provider."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider: str,
        enabled: bool,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: FrontierRetryConfig | None = None,
        error_mapper: ErrorMapper | None = None,
        sleeper: Sleeper | None = None,
        auth_scheme: str = "Bearer",
        extra_headers: dict[str, str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider = provider
        self._enabled = enabled
        self._retry = retry or FrontierRetryConfig()
        self._error_mapper = error_mapper or _default_error_mapper
        self._sleep = sleeper or _real_sleep
        self._auth_scheme = auth_scheme
        self._extra_headers = dict(extra_headers or {})
        self._rng = rng or random.Random()
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                self._retry.request_timeout_s,
                connect=self._retry.connect_timeout_s,
            ),
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def configured(self) -> bool:
        """True when both the transport flag is on and a key is present."""
        return self._enabled and bool(self._api_key)

    def url(self, path: str) -> str:
        """Join a path onto the base URL (absolute paths/URLs pass through)."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = dict(self._extra_headers)
        # Set the bearer Authorization unless the provider authenticates via a
        # different header it already supplied (auth_scheme="" opts out, e.g.
        # Google's x-goog-api-key).
        if self._api_key and self._auth_scheme:
            headers["Authorization"] = f"{self._auth_scheme} {self._api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _guard(self) -> None:
        if not self._enabled:
            raise FrontierTransportDisabled(
                "frontier video transport is disabled (FRONTIER_VIDEO_ENABLED is off); "
                "no network call issued",
                provider=self._provider,
            )

    # -- core retrying executor ------------------------------------------- #

    async def _execute(self, attempt: Callable[[], Awaitable[Any]], *, op: str) -> Any:
        last_error: FrontierError | None = None
        for attempt_no in range(1, self._retry.max_attempts + 1):
            try:
                return await attempt()
            except FrontierError as exc:
                last_error = exc
                if not exc.retryable or attempt_no >= self._retry.max_attempts:
                    raise
                delay = self._backoff(attempt_no, exc)
                logger.warning(
                    "frontier.transport_retry",
                    provider=self._provider,
                    op=op,
                    attempt=attempt_no,
                    reason=exc.code_enum.value,
                    delay_s=round(delay, 3),
                )
                await self._sleep(delay)
        assert last_error is not None  # pragma: no cover - loop always sets it
        raise last_error

    def _backoff(self, attempt_no: int, exc: FrontierError) -> float:
        retry_after = getattr(exc, "retry_after_s", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), self._retry.backoff_max_s)
        base = min(self._retry.backoff_base_s * (2 ** (attempt_no - 1)), self._retry.backoff_max_s)
        if self._retry.backoff_jitter_s:
            base += self._rng.uniform(0.0, self._retry.backoff_jitter_s)
        return base

    # -- public HTTP surface ---------------------------------------------- #

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        op: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Issue a JSON request; raise a typed FrontierError on fault/non-2xx."""
        self._guard()
        url = self.url(path)

        async def attempt() -> dict[str, Any]:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._headers(headers),
                )
            except httpx.TimeoutException as exc:
                raise build_error(
                    FrontierErrorCode.TIMEOUT,
                    f"{op} timed out",
                    provider=self._provider,
                ) from exc
            except httpx.HTTPError as exc:
                raise FrontierServerError(
                    f"transport error calling {op}: {exc}", provider=self._provider
                ) from exc
            return self._parse_json(resp)

        return await self._execute(attempt, op=op)

    def _parse_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            if resp.is_success:
                raise FrontierBadResponse(
                    f"{self._provider} response was not valid JSON",
                    provider=self._provider,
                    status_code=resp.status_code,
                ) from exc
            raise self._error_mapper(resp.status_code, resp.text[:200]) from exc
        if resp.is_success:
            if not isinstance(body, dict):
                raise FrontierBadResponse(
                    f"{self._provider} response was not a JSON object",
                    provider=self._provider,
                    status_code=resp.status_code,
                )
            return body
        # Attach the provider name onto the mapped error.
        err = self._error_mapper(resp.status_code, body)
        if err.provider is None:
            err.provider = self._provider
        raise err

    async def download(self, url: str, *, op: str = "frontier_download") -> bytes:
        """Download asset bytes (clip / last frame). Retries retryable faults."""
        self._guard()

        async def attempt() -> bytes:
            try:
                resp = await self._http.get(self.url(url))
            except httpx.TimeoutException as exc:
                raise build_error(
                    FrontierErrorCode.TIMEOUT,
                    f"{op} download timed out",
                    provider=self._provider,
                ) from exc
            except httpx.HTTPError as exc:
                raise FrontierServerError(
                    f"{op} download transport error: {exc}", provider=self._provider
                ) from exc
            if not resp.is_success:
                raise self._error_mapper(resp.status_code, f"{op} download failed")
            return resp.content

        return await self._execute(attempt, op=op)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FrontierTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "ErrorMapper",
    "FrontierRetryConfig",
    "FrontierTransport",
    "FrontierTransportDisabled",
    "Sleeper",
]
