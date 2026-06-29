"""Unit tests for the optimistic-concurrency retry policy + loop. Pure, with an
injected recording sleeper so backoff is asserted at zero wall-clock cost."""

from __future__ import annotations

import pytest

from app.eventsourcing.domain.concurrency import RetryPolicy, retry_on_conflict
from app.eventsourcing.store.protocol import ConcurrencyError


def test_delay_schedule() -> None:
    p = RetryPolicy(max_attempts=5, base_delay_s=0.1, factor=4.0, max_delay_s=1.0)
    assert p.delay_for(1) == 0.0  # first try, no delay
    assert p.delay_for(2) == pytest.approx(0.1)
    assert p.delay_for(3) == pytest.approx(0.4)
    assert p.delay_for(4) == pytest.approx(1.0)  # capped
    assert p.delay_for(5) == pytest.approx(1.0)


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="non-negative"):
        RetryPolicy(base_delay_s=-1)


async def test_retry_succeeds_after_transient_conflicts() -> None:
    attempts = 0
    slept: list[float] = []

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConcurrencyError("s", 0, attempts)
        return "ok"

    async def sleeper(d: float) -> None:
        slept.append(d)

    result = await retry_on_conflict(
        op,
        policy=RetryPolicy(max_attempts=5, base_delay_s=0.1),
        sleeper=sleeper,
    )
    assert result == "ok"
    assert attempts == 3
    # Two retries -> two backoff sleeps (before attempts 2 and 3).
    assert slept == [pytest.approx(0.1), pytest.approx(0.4)]


async def test_retry_exhausts_and_reraises() -> None:
    async def always_conflict() -> str:
        raise ConcurrencyError("s", 0, 1)

    async def sleeper(_d: float) -> None:
        return None

    with pytest.raises(ConcurrencyError):
        await retry_on_conflict(
            always_conflict,
            policy=RetryPolicy(max_attempts=3, base_delay_s=0.0),
            sleeper=sleeper,
        )


async def test_non_concurrency_error_propagates_immediately() -> None:
    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("business rule")

    with pytest.raises(ValueError, match="business rule"):
        await retry_on_conflict(op, policy=RetryPolicy(max_attempts=5))
    assert attempts == 1  # not retried


async def test_single_attempt_policy_does_not_retry() -> None:
    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        raise ConcurrencyError("s", 0, 1)

    with pytest.raises(ConcurrencyError):
        await retry_on_conflict(op, policy=RetryPolicy(max_attempts=1))
    assert attempts == 1
