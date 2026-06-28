"""Rate-limit + retry unit tests (§9.1 step 2) — deterministic, fake clock/sleep.

No real waiting: a manual monotonic clock and a sleep stub that advances it make
the token-bucket back-pressure and the retry backoff fully deterministic.
"""

from __future__ import annotations

import random

import pytest

from app.ingest.ratelimit import TokenBucket, is_transient, retrying


class _Clock:
    """A controllable monotonic clock; ``sleep`` advances it (no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.t += secs


# --------------------------------------------------------------------------- #
# TokenBucket
# --------------------------------------------------------------------------- #


async def test_disabled_bucket_never_sleeps() -> None:
    clock = _Clock()
    bucket = TokenBucket(0.0, 4, clock=clock.now, sleep=clock.sleep)
    for _ in range(100):
        await bucket.acquire()
    assert clock.sleeps == []


async def test_burst_then_throttle() -> None:
    clock = _Clock()
    bucket = TokenBucket(2.0, 3, clock=clock.now, sleep=clock.sleep)
    # The first 3 acquires consume the burst with no wait.
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == []
    # The 4th must wait ~1/rate = 0.5s for a token to refill.
    await bucket.acquire()
    assert clock.sleeps
    assert clock.sleeps[-1] == pytest.approx(0.5, abs=1e-6)


async def test_refill_over_time() -> None:
    clock = _Clock()
    bucket = TokenBucket(10.0, 1, clock=clock.now, sleep=clock.sleep)
    await bucket.acquire()  # spends the single token
    # Advance time by 1s out of band -> 10 tokens would refill (capped at 1).
    clock.t += 1.0
    await bucket.acquire()  # token available, no sleep
    assert len(clock.sleeps) == 0


# --------------------------------------------------------------------------- #
# is_transient
# --------------------------------------------------------------------------- #


def test_is_transient_markers() -> None:
    assert is_transient(RuntimeError("HTTP 429 Throttling.RateQuota"))
    assert is_transient(RuntimeError("connection reset by peer"))
    assert is_transient(TimeoutError())
    assert is_transient(ConnectionError())


def test_is_transient_rejects_logic_errors() -> None:
    assert not is_transient(ValueError("bad json"))
    assert not is_transient(KeyError("missing"))


# --------------------------------------------------------------------------- #
# retrying
# --------------------------------------------------------------------------- #


async def test_retrying_succeeds_first_try() -> None:
    calls = 0

    async def func() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    clock = _Clock()
    out = await retrying(func, sleep=clock.sleep)
    assert out == "ok"
    assert calls == 1
    assert clock.sleeps == []


async def test_retrying_recovers_after_transient() -> None:
    calls = 0

    async def func() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("429 Throttling.RateQuota")
        return "recovered"

    clock = _Clock()
    out = await retrying(
        func,
        max_attempts=4,
        base_delay_s=1.0,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    assert out == "recovered"
    assert calls == 3
    assert len(clock.sleeps) == 2  # two backoffs before the success


async def test_retrying_gives_up_after_max() -> None:
    calls = 0

    async def func() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("503 server error")

    clock = _Clock()
    with pytest.raises(RuntimeError):
        await retrying(func, max_attempts=3, sleep=clock.sleep, rng=random.Random(1))
    assert calls == 3


async def test_retrying_propagates_non_transient_immediately() -> None:
    calls = 0

    async def func() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("permanent logic bug")

    clock = _Clock()
    with pytest.raises(ValueError):
        await retrying(func, max_attempts=5, sleep=clock.sleep)
    assert calls == 1  # no retry on a non-transient error
    assert clock.sleeps == []


async def test_retrying_backoff_is_bounded() -> None:
    async def func() -> str:
        raise RuntimeError("timeout")

    seen: list[float] = []

    def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        seen.append(delay)

    clock = _Clock()
    with pytest.raises(RuntimeError):
        await retrying(
            func,
            max_attempts=5,
            base_delay_s=1.0,
            max_delay_s=4.0,
            sleep=clock.sleep,
            rng=random.Random(2),
            on_retry=on_retry,
        )
    # Full-jitter delays never exceed the per-attempt ceiling capped at max_delay.
    assert all(0.0 <= d <= 4.0 for d in seen)
    assert len(seen) == 4  # 5 attempts -> 4 backoffs
