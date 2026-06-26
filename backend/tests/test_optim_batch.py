"""Tests for app.optim.batch — bounded concurrency + transient-error backoff.

``gather_bounded``/``map_bounded`` coalesce independent provider calls (page analysis, identity,
keyframes) under a concurrency cap. ``with_backoff`` retries the provider layer's *retryable*
errors (``TransientProviderError`` / ``RateLimited``, incl. the image-model ``429
Throttling.RateQuota``) with exponential backoff, honoring a server ``retry_after_s`` when present.
Sleep is injected so tests are instant and deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from app.optim.batch import (
    default_should_retry,
    gather_bounded,
    map_bounded,
    with_backoff,
)
from app.providers.errors import (
    ProviderBadRequest,
    RateLimited,
    TransientProviderError,
)


async def test_gather_bounded_preserves_result_order() -> None:
    async def echo(i: int) -> int:
        await asyncio.sleep(0)
        return i

    assert await gather_bounded([echo(i) for i in range(6)], limit=2) == [0, 1, 2, 3, 4, 5]


async def test_gather_bounded_caps_concurrency() -> None:
    active = 0
    max_active = 0

    async def task(i: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return i

    results = await gather_bounded([task(i) for i in range(10)], limit=3)
    assert results == list(range(10))
    assert max_active <= 3


async def test_gather_bounded_rejects_nonpositive_limit() -> None:
    with pytest.raises(ValueError):
        await gather_bounded([], limit=0)


async def test_map_bounded_applies_fn_in_order() -> None:
    async def double(x: int) -> int:
        return x * 2

    assert await map_bounded(double, [1, 2, 3], limit=2) == [2, 4, 6]


async def test_with_backoff_returns_on_first_success_without_sleeping() -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    async def fn() -> str:
        return "ok"

    assert await with_backoff(fn, sleep=fake_sleep) == "ok"
    assert delays == []


async def test_with_backoff_retries_transient_then_succeeds_with_exponential_delays() -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientProviderError("blip")
        return "ok"

    out = await with_backoff(fn, retries=3, base_delay=0.5, sleep=fake_sleep)
    assert out == "ok"
    assert calls == 3
    assert delays == [0.5, 1.0]  # 0.5*2^0, 0.5*2^1


async def test_with_backoff_reraises_after_exhausting_retries() -> None:
    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> str:
        raise RateLimited("429 Throttling.RateQuota")

    with pytest.raises(RateLimited):
        await with_backoff(fn, retries=2, sleep=fake_sleep)


async def test_with_backoff_does_not_retry_non_retryable_errors() -> None:
    calls = 0

    async def fake_sleep(d: float) -> None:
        return None

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise ProviderBadRequest("bad params")

    with pytest.raises(ProviderBadRequest):
        await with_backoff(fn, retries=3, sleep=fake_sleep)
    assert calls == 1


async def test_with_backoff_honors_server_retry_after() -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimited("slow down", retry_after_s=2.5)
        return "ok"

    await with_backoff(fn, base_delay=0.5, sleep=fake_sleep)
    assert delays == [2.5]


async def test_with_backoff_caps_delay_at_max() -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise TransientProviderError("blip")
        return "ok"

    await with_backoff(fn, retries=5, base_delay=1.0, max_delay=4.0, sleep=fake_sleep)
    # 1, 2, 4, then capped at 4 (not 8).
    assert delays == [1.0, 2.0, 4.0, 4.0]


def test_default_should_retry_uses_provider_retryable_flag() -> None:
    assert default_should_retry(RateLimited("x")) is True
    assert default_should_retry(TransientProviderError("x")) is True
    assert default_should_retry(ProviderBadRequest("x")) is False
    assert default_should_retry(ValueError("x")) is False
