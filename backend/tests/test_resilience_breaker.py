"""Exhaustive circuit-breaker state-transition tests (clock injected, instant)."""

from __future__ import annotations

import pytest

from app.resilience.breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreaker,
)
from app.resilience.clock import ManualClock
from app.resilience.errors import CircuitOpen


def _breaker(clock: ManualClock, **cfg) -> CircuitBreaker:
    base = {
        "consecutive_failure_threshold": 3,
        "failure_rate_threshold": 1.0,  # disable rate trigger unless a test opts in
        "cooldown_s": 10.0,
        "half_open_max_calls": 1,
        "half_open_success_threshold": 1,
        "window_size": 20,
        "min_calls": 5,
    }
    base.update(cfg)
    return CircuitBreaker("dep", BreakerConfig(**base), clock=clock)


async def test_starts_closed_and_admits() -> None:
    br = _breaker(ManualClock())
    assert br.state is BreakerState.CLOSED
    await br.before_call()  # does not raise
    await br.record_success()
    assert br.state is BreakerState.CLOSED


async def test_consecutive_failures_trip_open() -> None:
    br = _breaker(ManualClock(), consecutive_failure_threshold=3)
    for _ in range(3):
        await br.before_call()
        await br.record_failure()
    assert br.state is BreakerState.OPEN
    assert br.snapshot().opened_count == 1


async def test_success_resets_consecutive_counter() -> None:
    br = _breaker(ManualClock(), consecutive_failure_threshold=3)
    await br.before_call()
    await br.record_failure()
    await br.before_call()
    await br.record_failure()
    await br.before_call()
    await br.record_success()  # resets the streak
    await br.before_call()
    await br.record_failure()
    assert br.state is BreakerState.CLOSED  # only 1 consecutive now


async def test_open_rejects_without_attempting() -> None:
    br = _breaker(ManualClock(), consecutive_failure_threshold=1)
    await br.before_call()
    await br.record_failure()
    assert br.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen) as ei:
        await br.before_call()
    assert ei.value.name == "dep"
    assert br.snapshot().total_rejections == 1


async def test_open_to_half_open_after_cooldown() -> None:
    clock = ManualClock()
    br = _breaker(clock, consecutive_failure_threshold=1, cooldown_s=10.0)
    await br.before_call()
    await br.record_failure()
    # Before cooldown: still rejecting.
    clock.advance(9.0)
    with pytest.raises(CircuitOpen):
        await br.before_call()
    # After cooldown: admits a probe (HALF_OPEN).
    clock.advance(2.0)
    await br.before_call()
    assert br.state is BreakerState.HALF_OPEN


async def test_half_open_success_closes() -> None:
    clock = ManualClock()
    br = _breaker(clock, consecutive_failure_threshold=1, cooldown_s=5.0)
    await br.before_call()
    await br.record_failure()
    clock.advance(6.0)
    await br.before_call()  # HALF_OPEN probe
    await br.record_success()
    assert br.state is BreakerState.CLOSED


async def test_half_open_failure_reopens() -> None:
    clock = ManualClock()
    br = _breaker(clock, consecutive_failure_threshold=1, cooldown_s=5.0)
    await br.before_call()
    await br.record_failure()
    clock.advance(6.0)
    await br.before_call()  # probe
    await br.record_failure()
    assert br.state is BreakerState.OPEN


async def test_half_open_probe_budget_enforced() -> None:
    clock = ManualClock()
    br = _breaker(
        clock, consecutive_failure_threshold=1, cooldown_s=5.0, half_open_max_calls=1
    )
    await br.before_call()
    await br.record_failure()
    clock.advance(6.0)
    await br.before_call()  # takes the only probe slot
    with pytest.raises(CircuitOpen):
        await br.before_call()  # budget exhausted


async def test_half_open_needs_threshold_successes() -> None:
    clock = ManualClock()
    br = _breaker(
        clock,
        consecutive_failure_threshold=1,
        cooldown_s=5.0,
        half_open_max_calls=2,
        half_open_success_threshold=2,
    )
    await br.before_call()
    await br.record_failure()
    clock.advance(6.0)
    await br.before_call()
    await br.before_call()  # two probes admitted
    await br.record_success()
    assert br.state is BreakerState.HALF_OPEN  # one success, not enough
    await br.record_success()
    assert br.state is BreakerState.CLOSED


async def test_failure_rate_trigger() -> None:
    clock = ManualClock()
    # 50% failure rate over a window of 10, min 6 calls.
    br = _breaker(
        clock,
        consecutive_failure_threshold=0,  # disable consecutive trigger
        failure_rate_threshold=0.5,
        window_size=10,
        min_calls=6,
    )
    # 3 success, 3 fail interleaved => 6 calls, rate 0.5 -> trips on the 6th.
    for _ in range(3):
        await br.before_call()
        await br.record_success()
    for i in range(3):
        await br.before_call()
        await br.record_failure()
        if i < 2:
            assert br.state is BreakerState.CLOSED
    assert br.state is BreakerState.OPEN


async def test_rate_trigger_waits_for_min_calls() -> None:
    clock = ManualClock()
    br = _breaker(
        clock,
        consecutive_failure_threshold=0,
        failure_rate_threshold=0.5,
        window_size=10,
        min_calls=5,
    )
    # 2 failures: rate 1.0 but only 2 calls < min_calls=5 => stay closed.
    for _ in range(2):
        await br.before_call()
        await br.record_failure()
    assert br.state is BreakerState.CLOSED


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        BreakerConfig(failure_rate_threshold=1.5)
    with pytest.raises(ValueError):
        BreakerConfig(window_size=0)
    with pytest.raises(ValueError):
        BreakerConfig(half_open_success_threshold=2, half_open_max_calls=1)
    with pytest.raises(ValueError):
        # both triggers disabled is nonsensical
        BreakerConfig(consecutive_failure_threshold=0, failure_rate_threshold=1.0)


async def test_snapshot_counts() -> None:
    br = _breaker(ManualClock(), consecutive_failure_threshold=2)
    await br.before_call()
    await br.record_success()
    await br.before_call()
    await br.record_failure()
    snap = br.snapshot()
    assert snap.total_successes == 1
    assert snap.total_failures == 1
    assert snap.name == "dep"
