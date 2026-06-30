"""Integration tests for resilient_call / ResiliencePolicy — the full stack.

Proves the layers compose in the documented order and interact correctly: retry sees
breaker rejections as retryable, the breaker scores per-attempt outcomes, the
bulkhead isolates concurrency, the rate limiter and chaos sit inside the loop. All
time is virtual.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from app.resilience.backoff import BackoffPolicy, JitterStrategy
from app.resilience.breaker import BreakerConfig, BreakerState, CircuitBreaker
from app.resilience.bulkhead import Bulkhead, BulkheadConfig
from app.resilience.chaos import ChaosConfig, ChaosFault, ChaosMonkey
from app.resilience.clock import ManualClock
from app.resilience.composite import ResiliencePolicy, resilient, resilient_call
from app.resilience.errors import (
    BulkheadFull,
    CallTimeout,
    PermanentError,
    RetriesExhausted,
    TransientError,
)
from app.resilience.ratelimit import TokenBucket, TokenBucketConfig
from app.resilience.retry import RetryPolicy

_FIXED = BackoffPolicy(base_s=1.0, max_s=10.0, strategy=JitterStrategy.NONE)


def _flaky(fail_times: int, exc=lambda: TransientError("blip")):
    state = {"n": 0}

    async def fn() -> str:
        if state["n"] < fail_times:
            state["n"] += 1
            raise exc()
        return "ok"

    return fn, state


async def test_bare_resilient_call_retries_default() -> None:
    clock = ManualClock()
    fn, _ = _flaky(1)
    policy = ResiliencePolicy(
        name="t",
        clock=clock,
        retry=RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock),
    )
    assert await resilient_call(fn, policy) == "ok"
    assert clock.slept == [1.0]


async def test_retry_plus_breaker_scoring() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "dep", BreakerConfig(consecutive_failure_threshold=5), clock=clock
    )
    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=4, backoff=_FIXED, clock=clock),
        breaker=breaker,
        clock=clock,
    )
    fn, _ = _flaky(2)
    assert await resilient_call(fn, policy) == "ok"
    snap = breaker.snapshot()
    assert snap.total_failures == 2
    assert snap.total_successes == 1
    assert breaker.state is BreakerState.CLOSED


async def test_breaker_open_short_circuits_and_is_retryable() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "dep",
        BreakerConfig(consecutive_failure_threshold=2, cooldown_s=100.0),
        clock=clock,
    )
    # Pre-trip the breaker so before_call rejects.
    for _ in range(2):
        await breaker.before_call()
        await breaker.record_failure()
    assert breaker.state is BreakerState.OPEN

    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        return "ok"

    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock),
        breaker=breaker,
        clock=clock,
    )
    # CircuitOpen is retryable, so the loop retries but the breaker keeps rejecting
    # (cooldown not elapsed) => RetriesExhausted, and fn never actually ran.
    with pytest.raises(RetriesExhausted):
        await resilient_call(fn, policy)
    assert calls["n"] == 0


async def test_non_retryable_stops_immediately() -> None:
    clock = ManualClock()
    fn, state = _flaky(99, exc=lambda: PermanentError("hard"))
    policy = ResiliencePolicy(
        name="t",
        retry=RetryPolicy(max_attempts=5, backoff=_FIXED, clock=clock),
        clock=clock,
    )
    with pytest.raises(PermanentError):
        await resilient_call(fn, policy)
    assert state["n"] == 1
    assert clock.slept == []


async def test_timeout_layer_makes_breaker_count_a_hang() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "dep", BreakerConfig(consecutive_failure_threshold=10), clock=clock
    )

    async def hang() -> None:
        await asyncio.Event().wait()

    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=1, backoff=_FIXED, clock=clock),
        timeout_s=0.05,
        breaker=breaker,
        clock=clock,
    )
    with pytest.raises(CallTimeout):  # single-attempt policy surfaces the original
        await resilient_call(hang, policy)
    assert breaker.snapshot().total_failures == 1


async def test_bulkhead_isolates_concurrency() -> None:
    bh = Bulkhead("dep", BulkheadConfig(max_concurrency=1, max_queue=0))
    clock = ManualClock()
    release = asyncio.Event()

    async def hold() -> str:
        await release.wait()
        return "done"

    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=1, backoff=_FIXED, clock=clock),
        bulkhead=bh,
        clock=clock,
    )
    t1 = asyncio.create_task(resilient_call(hold, policy))
    await asyncio.sleep(0)
    assert bh.active == 1
    # Second call is shed because the bulkhead is full + max_attempts=1.
    with pytest.raises(BulkheadFull):
        await resilient_call(hold, policy)
    release.set()
    assert await t1 == "done"


async def test_rate_limiter_acquired_per_attempt() -> None:
    clock = ManualClock()
    tb = TokenBucket("dep", TokenBucketConfig(rate=1.0, burst=2.0), clock=clock)

    async def fn() -> str:
        return "ok"

    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=1, backoff=_FIXED, clock=clock),
        rate_limiter=tb,
        clock=clock,
    )
    await resilient_call(fn, policy)
    await resilient_call(fn, policy)
    # Two tokens consumed from the burst of 2.
    assert tb.available_tokens < 1.0


async def test_chaos_injected_transient_fault_is_retried() -> None:
    clock = ManualClock()
    # Always-on chaos transient fault: the call body never even runs, every attempt
    # fails on the injected fault, and because the fault is retryable the loop tries
    # all attempts before giving up — proving chaos faults flow through the policy.
    monkey = ChaosMonkey(
        "dep",
        ChaosConfig(fault_probability=1.0, fault_weights={ChaosFault.TRANSIENT: 1.0}),
        enabled=True,
        rng=random.Random(0),
    )
    body_ran = {"n": 0}

    async def fn() -> str:
        body_ran["n"] += 1
        return "ok"

    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock),
        chaos=monkey,
        clock=clock,
    )
    with pytest.raises(RetriesExhausted):
        await resilient_call(fn, policy)
    assert monkey.faults_injected == 3  # one per attempt
    assert body_ran["n"] == 0  # fault fired before the body each time
    assert clock.slept == [1.0, 2.0]  # two retries between three attempts


async def test_chaos_disabled_is_transparent() -> None:
    clock = ManualClock()
    monkey = ChaosMonkey(
        "dep",
        ChaosConfig(fault_probability=1.0),
        enabled=False,  # disabled => never injects
        rng=random.Random(0),
    )
    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=2, backoff=_FIXED, clock=clock),
        chaos=monkey,
        clock=clock,
    )

    async def fn() -> str:
        return "ok"

    assert await resilient_call(fn, policy) == "ok"
    assert monkey.faults_injected == 0


async def test_decorator_form_end_to_end() -> None:
    clock = ManualClock()
    calls = {"n": 0}
    policy = ResiliencePolicy(
        name="dep",
        retry=RetryPolicy(max_attempts=3, backoff=_FIXED, clock=clock),
        clock=clock,
    )

    @resilient(policy)
    async def fetch(x: int) -> int:
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientError("blip")
        return x + 1

    assert await fetch(41) == 42
    assert calls["n"] == 2


async def test_effective_retry_default_when_none() -> None:
    # No explicit retry => _effective_retry() builds a default 3-attempt policy on
    # the default predicate. We avoid any wall sleep by succeeding on the first try
    # (so the default's SYSTEM_CLOCK is never asked to sleep).
    fn, state = _flaky(0)
    policy = ResiliencePolicy(name="t")  # everything default
    assert policy.retry is None
    assert await resilient_call(fn, policy) == "ok"
    assert state["n"] == 0
