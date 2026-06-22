"""Unit tests for the shared provider client: rate limiter, circuit breaker,
retry/backoff, HTTP + SDK transport, error classification, and cost accounting.

No network: HTTP goes through ``httpx.MockTransport``; SDK calls are faked.
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import (
    BreakerState,
    CircuitBreaker,
    LoggingUsageSink,
    ProviderClient,
    ResilienceConfig,
    TokenBucket,
    classify_status,
)
from app.providers.errors import (
    AuthenticationError,
    CircuitOpenError,
    ModelNotAvailable,
    ProviderBadRequest,
    ProviderError,
    RateLimited,
    TransientProviderError,
)
from app.providers.types import Usage

FAST = ResilienceConfig(
    max_attempts=3,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
    backoff_jitter_s=0.0,
    breaker_failure_threshold=3,
    breaker_recovery_s=0.05,
    rate_per_s=1000.0,
    rate_burst=1000,
)


def make_settings(**overrides: object) -> Settings:
    return Settings(dashscope_api_key="test", **overrides)  # type: ignore[arg-type]


def make_client(
    handler: object, *, resilience: ResilienceConfig | None = None, **kw: object
) -> ProviderClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return ProviderClient(make_settings(), transport=transport, resilience=resilience or FAST, **kw)  # type: ignore[arg-type]


class _Counter:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


async def test_token_bucket_throttles_when_empty() -> None:
    bucket = TokenBucket(rate_per_s=50.0, burst=1)
    await bucket.acquire()  # consumes the single burst token immediately
    start = time.monotonic()
    await bucket.acquire()  # must wait ~1/50s for a refill
    assert time.monotonic() - start >= 0.012


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


async def test_circuit_breaker_opens_then_half_opens_then_closes() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_s=0.05)
    await breaker.before_call()  # closed: ok
    await breaker.record_failure()
    await breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.before_call()
    time.sleep(0.06)
    await breaker.before_call()  # cool-down elapsed -> half-open probe allowed
    assert breaker.state is BreakerState.HALF_OPEN
    await breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    await breaker.before_call()


async def test_half_open_failure_reopens() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=0.0)
    await breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    await breaker.before_call()  # recovery 0 -> half-open
    assert breaker.state is BreakerState.HALF_OPEN
    await breaker.record_failure()  # probe failed -> open again
    assert breaker.state is BreakerState.OPEN


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #


def test_classify_status_mapping() -> None:
    assert isinstance(classify_status(429), RateLimited)
    assert isinstance(classify_status(503), TransientProviderError)
    assert isinstance(classify_status(401), AuthenticationError)
    assert isinstance(
        classify_status(400, code="InvalidParameter", message="Model not exist."),
        ModelNotAvailable,
    )
    assert isinstance(classify_status(400, message="bad shape"), ProviderBadRequest)


# --------------------------------------------------------------------------- #
# HTTP transport: retry / no-retry / circuit opening
# --------------------------------------------------------------------------- #


async def test_request_json_retries_transient_then_succeeds() -> None:
    counter = _Counter(
        [
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = make_client(counter)
    body = await client.request_json("GET", "https://x.test/v", op="t", model="m")
    assert body == {"ok": True}
    assert counter.calls == 3
    await client.aclose()


async def test_request_json_bad_request_not_retried() -> None:
    counter = _Counter(
        [httpx.Response(400, json={"code": "InvalidParameter", "message": "Model not exist."})]
    )
    client = make_client(counter)
    with pytest.raises(ModelNotAvailable):
        await client.request_json("POST", "https://x.test/v", op="t", model="m")
    assert counter.calls == 1
    await client.aclose()


async def test_request_json_rate_limited_is_retried() -> None:
    counter = _Counter(
        [httpx.Response(429, json={"message": "slow down"}), httpx.Response(200, json={"ok": 1})]
    )
    client = make_client(counter)
    assert await client.request_json("GET", "https://x.test/v", op="t", model="m") == {"ok": 1}
    assert counter.calls == 2
    await client.aclose()


async def test_compat_mode_error_envelope_is_parsed() -> None:
    counter = _Counter(
        [httpx.Response(401, json={"error": {"code": "InvalidApiKey", "message": "bad key"}})]
    )
    client = make_client(counter)
    with pytest.raises(AuthenticationError):
        await client.request_json("POST", "https://x.test/v", op="t", model="m")
    await client.aclose()


async def test_circuit_opens_after_repeated_failures() -> None:
    counter = _Counter([httpx.Response(500, json={"message": "down"})])
    client = make_client(counter)  # threshold=3, max_attempts=3 -> one call trips it
    with pytest.raises(TransientProviderError):
        await client.request_json("GET", "https://x.test/v", op="t", model="m")
    # Breaker is now open: the next call is rejected without any HTTP attempt.
    calls_before = counter.calls
    with pytest.raises(CircuitOpenError):
        await client.request_json("GET", "https://x.test/v", op="t", model="m")
    assert counter.calls == calls_before
    await client.aclose()


# --------------------------------------------------------------------------- #
# Cost accounting
# --------------------------------------------------------------------------- #


def test_logging_usage_sink_accumulates() -> None:
    sink = LoggingUsageSink()
    sink(Usage(model="m", operation="chat", input_tokens=10, output_tokens=5))
    sink(Usage(model="m", operation="video", video_seconds=5.0))
    assert sink.totals.events == 2
    assert sink.totals.total_tokens == 15
    assert sink.totals.video_seconds == 5.0
    assert sink.totals.by_operation == {"chat": 1, "video": 1}


async def test_custom_usage_sink_receives_events() -> None:
    seen: list[Usage] = []
    client = make_client(_Counter([httpx.Response(200, json={})]), usage_sink=seen.append)
    client.record_usage(Usage(model="m", operation="image", images=2))
    assert seen and seen[0].images == 2
    assert client.usage_totals is None  # custom sink -> no default accumulator
    await client.aclose()


async def test_broken_usage_sink_never_raises() -> None:
    def boom(_usage: Usage) -> None:
        raise RuntimeError("sink down")

    client = make_client(_Counter([httpx.Response(200, json={})]), usage_sink=boom)
    client.record_usage(Usage(model="m", operation="chat"))  # must not raise
    await client.aclose()


# --------------------------------------------------------------------------- #
# SDK transport
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(
        self, status_code: int = 200, code: str | None = None, message: str | None = None
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = "req-1"
        self.output = {"value": 42}


async def test_call_sdk_returns_on_ok() -> None:
    client = make_client(_Counter([httpx.Response(200, json={})]))
    result = await client.call_sdk(lambda: _FakeResp(200), op="image", model="m")
    assert result.output["value"] == 42
    await client.aclose()


async def test_call_sdk_classifies_non_ok_response() -> None:
    client = make_client(_Counter([httpx.Response(200, json={})]))
    with pytest.raises(ModelNotAvailable):
        await client.call_sdk(
            lambda: _FakeResp(400, code="InvalidParameter", message="Model not exist."),
            op="image",
            model="m",
        )
    await client.aclose()


async def test_call_sdk_retries_transient_sdk_exception() -> None:
    attempts = {"n": 0}

    def flaky() -> _FakeResp:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TimeoutError("connection timed out")
        return _FakeResp(200)

    client = make_client(_Counter([httpx.Response(200, json={})]))
    result = await client.call_sdk(flaky, op="video", model="m")
    assert result.status_code == 200
    assert attempts["n"] == 2
    await client.aclose()


async def test_call_sdk_does_not_retry_programming_error() -> None:
    def bad() -> _FakeResp:
        raise ValueError("a real bug")

    client = make_client(_Counter([httpx.Response(200, json={})]))
    with pytest.raises(ProviderError):
        await client.call_sdk(bad, op="video", model="m")
    await client.aclose()
