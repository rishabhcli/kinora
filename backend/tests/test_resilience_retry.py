"""Deterministic tests for the retry policy — decorator, context manager, deadline.

All time is virtual (ManualClock.sleep advances instead of waiting), so multi-second
backoff ladders run instantly. The seeded RNG makes the exact sleep durations fixed.
"""

from __future__ import annotations

import pytest

from app.resilience.backoff import BackoffPolicy, JitterStrategy
from app.resilience.clock import ManualClock
from app.resilience.errors import (
    DeadlineExceeded,
    PermanentError,
    RateLimitedError,
    RetriesExhausted,
    TransientError,
)
from app.resilience.retry import RetryAttempt, RetryPolicy, retryable

# A deterministic (no-jitter) backoff so sleep durations are exactly known.
_FIXED = BackoffPolicy(base_s=1.0, max_s=100.0, multiplier=2.0, strategy=JitterStrategy.NONE)


def _flaky(fail_times: int, exc_factory=lambda: TransientError("blip")):
    """A coroutine factory that raises ``fail_times`` then returns 'ok'."""
    state = {"n": 0}

    async def _fn() -> str:
        if state["n"] < fail_times:
            state["n"] += 1
            raise exc_factory()
        return "ok"

    return _fn, state


async def test_succeeds_first_try_no_sleep() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock)
    fn, state = _flaky(0)
    assert await policy.execute(fn) == "ok"
    assert state["n"] == 0
    assert clock.slept == []


async def test_retries_then_succeeds_with_expected_backoff() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=5, backoff=_FIXED, clock=clock)
    fn, _ = _flaky(2)
    assert await policy.execute(fn) == "ok"
    # Two failures => two sleeps following the NONE exponential ladder: 1s, 2s.
    assert clock.slept == [1.0, 2.0]


async def test_exhaustion_raises_retries_exhausted_with_cause() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock)
    fn, _ = _flaky(99)
    with pytest.raises(RetriesExhausted) as ei:
        await policy.execute(fn)
    assert ei.value.attempts == 3
    assert isinstance(ei.value.cause, TransientError)
    # 3 attempts => 2 sleeps.
    assert clock.slept == [1.0, 2.0]


async def test_non_retryable_raises_immediately_no_sleep() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=5, backoff=_FIXED, clock=clock)
    fn, state = _flaky(99, exc_factory=lambda: PermanentError("nope"))
    with pytest.raises(PermanentError):
        await policy.execute(fn)
    assert state["n"] == 1  # attempted once, no retry
    assert clock.slept == []


async def test_retry_on_exception_class_filter() -> None:
    clock = ManualClock()
    policy = RetryPolicy(
        max_attempts=5, backoff=_FIXED, retry_on=ValueError, clock=clock
    )
    fn, _ = _flaky(2, exc_factory=lambda: ValueError("retry me"))
    assert await policy.execute(fn) == "ok"
    # A non-ValueError must NOT be retried even though it's "transient".
    fn2, state2 = _flaky(99, exc_factory=lambda: TransientError("not a ValueError"))
    with pytest.raises(TransientError):
        await policy.execute(fn2)
    assert state2["n"] == 1


async def test_retry_on_predicate() -> None:
    clock = ManualClock()
    policy = RetryPolicy(
        max_attempts=4,
        backoff=_FIXED,
        retry_on=lambda e: "transient" in str(e),
        clock=clock,
    )
    fn, _ = _flaky(2, exc_factory=lambda: RuntimeError("transient glitch"))
    assert await policy.execute(fn) == "ok"


async def test_deadline_budget_stops_before_oversleeping() -> None:
    clock = ManualClock()
    # Budget 2.5s: after attempt 1 (sleep 1s, elapsed 0+1<=2.5 ok), attempt 2 fails
    # and the next sleep would be 2s with elapsed 1s => 3s > 2.5 => DeadlineExceeded.
    policy = RetryPolicy(max_attempts=10, backoff=_FIXED, deadline_s=2.5, clock=clock)
    fn, _ = _flaky(99)
    with pytest.raises(DeadlineExceeded) as ei:
        await policy.execute(fn)
    assert ei.value.attempts == 2
    assert clock.slept == [1.0]  # only the first backoff happened


async def test_on_retry_hook_called_per_retry() -> None:
    clock = ManualClock()
    seen: list[RetryAttempt] = []
    policy = RetryPolicy(
        max_attempts=4, backoff=_FIXED, on_retry=seen.append, clock=clock
    )
    fn, _ = _flaky(2)
    await policy.execute(fn)
    assert [a.number for a in seen] == [1, 2]
    assert [a.delay_s for a in seen] == [1.0, 2.0]
    assert all(isinstance(a.exception, TransientError) for a in seen)


async def test_on_retry_hook_exception_does_not_break_loop() -> None:
    clock = ManualClock()

    def _boom(_a: RetryAttempt) -> None:
        raise RuntimeError("hook bug")

    policy = RetryPolicy(max_attempts=3, backoff=_FIXED, on_retry=_boom, clock=clock)
    fn, _ = _flaky(1)
    assert await policy.execute(fn) == "ok"  # hook blew up but loop survived


async def test_retry_after_hint_drives_backoff_floor() -> None:
    clock = ManualClock()
    # FULL jitter capped tiny, but the server asks for 4s; the sleep must be >= 4s.
    policy = RetryPolicy(
        max_attempts=2,
        backoff=BackoffPolicy(base_s=0.01, max_s=0.01, strategy=JitterStrategy.FULL),
        clock=clock,
    )
    fn, _ = _flaky(1, exc_factory=lambda: RateLimitedError("429", retry_after_s=4.0))
    await policy.execute(fn)
    assert clock.slept[0] >= 4.0


async def test_decorator_form() -> None:
    clock = ManualClock()
    calls = {"n": 0}

    @retryable(max_attempts=3, backoff=_FIXED, clock=clock)
    async def fetch(x: int) -> int:
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientError("blip")
        return x * 2

    assert await fetch(21) == 42
    assert calls["n"] == 2
    assert clock.slept == [1.0]


async def test_context_manager_form_retries() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=4, backoff=_FIXED, clock=clock)
    attempts = {"n": 0}
    result = None
    async for attempt in policy.attempt():
        with attempt:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientError("blip")
            result = "done"
    assert result == "done"
    assert attempts["n"] == 3
    assert clock.slept == [1.0, 2.0]


async def test_context_manager_form_exhausts() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=2, backoff=_FIXED, clock=clock)
    with pytest.raises(RetriesExhausted):
        async for attempt in policy.attempt():
            with attempt:
                raise TransientError("always")


async def test_context_manager_non_retryable_propagates() -> None:
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=5, backoff=_FIXED, clock=clock)
    with pytest.raises(PermanentError):
        async for attempt in policy.attempt():
            with attempt:
                raise PermanentError("hard fail")


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(deadline_s=0.0)


async def test_seeded_jitter_is_reproducible_across_runs() -> None:
    clock_a = ManualClock()
    clock_b = ManualClock()
    pol_a = RetryPolicy(
        max_attempts=5,
        backoff=BackoffPolicy(strategy=JitterStrategy.FULL, base_s=0.5, max_s=10.0),
        clock=clock_a,
    )
    pol_b = RetryPolicy(
        max_attempts=5,
        backoff=BackoffPolicy(strategy=JitterStrategy.FULL, base_s=0.5, max_s=10.0),
        clock=clock_b,
    )
    fn_a, _ = _flaky(3)
    fn_b, _ = _flaky(3)
    await pol_a.execute(fn_a, rng_seed=42)
    await pol_b.execute(fn_b, rng_seed=42)
    assert clock_a.slept == clock_b.slept
