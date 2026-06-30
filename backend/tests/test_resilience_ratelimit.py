"""Tests for the client-side rate limiters (token bucket + sliding window).

Time is injected via ManualClock; ``acquire`` blocking is exercised by advancing the
clock between iterations, so nothing waits on the wall clock.
"""

from __future__ import annotations

import asyncio

import pytest

from app.resilience.clock import ManualClock
from app.resilience.errors import RateLimitExceeded
from app.resilience.ratelimit import (
    SlidingWindowConfig,
    SlidingWindowLimiter,
    TokenBucket,
    TokenBucketConfig,
)

# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #


async def test_token_bucket_allows_burst_then_blocks() -> None:
    clock = ManualClock()
    tb = TokenBucket("dep", TokenBucketConfig(rate=1.0, burst=3.0), clock=clock)
    # Burst of 3 available immediately.
    for _ in range(3):
        assert await tb.acquire(block=False) is True
    # 4th is denied (non-blocking).
    with pytest.raises(RateLimitExceeded):
        await tb.acquire(block=False)


async def test_token_bucket_refills_over_time() -> None:
    clock = ManualClock()
    tb = TokenBucket("dep", TokenBucketConfig(rate=2.0, burst=2.0), clock=clock)
    await tb.acquire()
    await tb.acquire()
    assert tb.try_acquire() is False
    clock.advance(0.5)  # 0.5s * 2/s = 1 token
    assert tb.try_acquire() is True


async def test_token_bucket_blocking_acquire_waits_via_clock() -> None:
    clock = ManualClock()
    tb = TokenBucket("dep", TokenBucketConfig(rate=4.0, burst=1.0), clock=clock)
    await tb.acquire()  # drains the single token

    async def drive() -> None:
        # While the acquire is parked on clock.sleep, advance time to refill.
        for _ in range(10):
            await asyncio.sleep(0)
            clock.advance(0.1)

    driver = asyncio.create_task(drive())
    await tb.acquire()  # needs 1 token at 4/s => ~0.25s of virtual time
    await driver
    assert clock.slept  # it did sleep (virtually)


async def test_token_bucket_rejects_request_above_capacity() -> None:
    tb = TokenBucket("dep", TokenBucketConfig(rate=1.0, burst=2.0))
    with pytest.raises(ValueError):
        await tb.acquire(5.0)


def test_token_bucket_config_validation() -> None:
    with pytest.raises(ValueError):
        TokenBucketConfig(rate=0.0)
    with pytest.raises(ValueError):
        TokenBucketConfig(burst=0.0)


# --------------------------------------------------------------------------- #
# Sliding window
# --------------------------------------------------------------------------- #


async def test_sliding_window_caps_events_in_window() -> None:
    clock = ManualClock()
    sw = SlidingWindowLimiter(
        "dep", SlidingWindowConfig(max_events=3, window_s=10.0), clock=clock
    )
    for _ in range(3):
        assert await sw.acquire(block=False) is True
    with pytest.raises(RateLimitExceeded):
        await sw.acquire(block=False)
    assert sw.current_count == 3


async def test_sliding_window_evicts_aged_events() -> None:
    clock = ManualClock()
    sw = SlidingWindowLimiter(
        "dep", SlidingWindowConfig(max_events=2, window_s=10.0), clock=clock
    )
    await sw.acquire()
    await sw.acquire()
    assert sw.try_acquire() is False
    clock.advance(10.1)  # both events age out
    assert sw.current_count == 0
    assert sw.try_acquire() is True


async def test_sliding_window_blocking_waits_until_slot_frees() -> None:
    clock = ManualClock()
    sw = SlidingWindowLimiter(
        "dep", SlidingWindowConfig(max_events=1, window_s=5.0), clock=clock
    )
    await sw.acquire()  # window now full

    async def drive() -> None:
        for _ in range(10):
            await asyncio.sleep(0)
            clock.advance(1.0)

    driver = asyncio.create_task(drive())
    await sw.acquire()  # blocks until the first event ages out (~5s virtual)
    await driver


async def test_sliding_window_rejects_request_above_cap() -> None:
    sw = SlidingWindowLimiter("dep", SlidingWindowConfig(max_events=2, window_s=5.0))
    with pytest.raises(ValueError):
        await sw.acquire(5.0)


def test_sliding_window_config_validation() -> None:
    with pytest.raises(ValueError):
        SlidingWindowConfig(max_events=0)
    with pytest.raises(ValueError):
        SlidingWindowConfig(window_s=0.0)
