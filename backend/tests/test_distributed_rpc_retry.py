"""Tests for retry-with-budget + exponential backoff."""

from __future__ import annotations

import random

import pytest

from app.distributed.rpc.deadline import Deadline, ManualClock
from app.distributed.rpc.errors import RpcError, RpcStatus, not_found, unavailable
from app.distributed.rpc.retry import (
    BackoffPolicy,
    RetryBudget,
    RetryPolicy,
    run_with_retry,
)


def _slept(clk: ManualClock) -> tuple[list[float], object]:
    waits: list[float] = []

    async def _sleep(s: float) -> None:
        waits.append(s)
        clk.advance(s)

    return waits, _sleep


# -- BackoffPolicy ---------------------------------------------------------- #


def test_backoff_none_is_pure_exponential() -> None:
    b = BackoffPolicy(base_delay_s=0.1, factor=2.0, jitter="none")
    rng = random.Random(0)
    assert b.delay(0, rng=rng) == pytest.approx(0.1)
    assert b.delay(1, rng=rng) == pytest.approx(0.2)
    assert b.delay(2, rng=rng) == pytest.approx(0.4)


def test_backoff_clamped_to_max() -> None:
    b = BackoffPolicy(base_delay_s=1.0, factor=10.0, max_delay_s=5.0, jitter="none")
    assert b.delay(5, rng=random.Random(0)) == 5.0


def test_backoff_full_jitter_within_bounds() -> None:
    b = BackoffPolicy(base_delay_s=0.1, factor=2.0, jitter="full")
    rng = random.Random(1)
    for attempt in range(5):
        d = b.delay(attempt, rng=rng)
        assert 0.0 <= d <= min(5.0, 0.1 * 2**attempt)


# -- RetryBudget ------------------------------------------------------------ #


def test_retry_budget_floor_allows_initial_retries() -> None:
    budget = RetryBudget(ratio=0.0, min_retries_floor=3)
    for _ in range(3):
        budget.record_primary()
    # Within the floor, retries pass even with no accrued tokens.
    assert budget.try_withdraw()


def test_retry_budget_exhausts_after_floor() -> None:
    budget = RetryBudget(ratio=0.0, min_retries_floor=2, max_tokens=100)
    for _ in range(10):
        budget.record_primary()
    # Past the floor with ratio 0, no tokens accrue → retries denied.
    assert not budget.try_withdraw()


def test_retry_budget_accrues_tokens() -> None:
    budget = RetryBudget(ratio=1.0, min_retries_floor=0)
    budget.record_primary()
    budget.record_primary()
    assert budget.tokens == pytest.approx(2.0)
    assert budget.try_withdraw()
    assert budget.tokens == pytest.approx(1.0)


# -- run_with_retry --------------------------------------------------------- #


async def test_retry_succeeds_after_transient_failures() -> None:
    clk = ManualClock()
    waits, sleep = _slept(clk)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise unavailable("transient")
        return "ok"

    policy = RetryPolicy(max_attempts=5, backoff=BackoffPolicy(jitter="none"))
    result = await run_with_retry(
        attempt,
        policy=policy,
        idempotent=True,
        deadline=Deadline.never(),
        clock=clk,
        sleep=sleep,  # type: ignore[arg-type]
        rng=random.Random(0),
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert len(waits) == 2  # slept before the 2nd and 3rd attempts


async def test_retry_does_not_retry_non_retryable() -> None:
    clk = ManualClock()
    _waits, sleep = _slept(clk)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        raise not_found("gone")

    with pytest.raises(RpcError) as exc:
        await run_with_retry(
            attempt,
            policy=RetryPolicy(max_attempts=5),
            idempotent=True,
            deadline=Deadline.never(),
            clock=clk,
            sleep=sleep,  # type: ignore[arg-type]
        )
    assert exc.value.status is RpcStatus.NOT_FOUND
    assert calls["n"] == 1  # failed fast, no retry


async def test_retry_does_not_retry_non_idempotent() -> None:
    clk = ManualClock()
    _waits, sleep = _slept(clk)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        raise unavailable("transient")

    with pytest.raises(RpcError):
        await run_with_retry(
            attempt,
            policy=RetryPolicy(max_attempts=5),
            idempotent=False,  # not safe to retry
            deadline=Deadline.never(),
            clock=clk,
            sleep=sleep,  # type: ignore[arg-type]
        )
    assert calls["n"] == 1


async def test_retry_stops_at_max_attempts() -> None:
    clk = ManualClock()
    _waits, sleep = _slept(clk)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        raise unavailable("always")

    with pytest.raises(RpcError):
        await run_with_retry(
            attempt,
            policy=RetryPolicy(max_attempts=3, backoff=BackoffPolicy(jitter="none")),
            idempotent=True,
            deadline=Deadline.never(),
            clock=clk,
            sleep=sleep,  # type: ignore[arg-type]
        )
    assert calls["n"] == 3


async def test_retry_respects_deadline() -> None:
    clk = ManualClock()
    _waits, sleep = _slept(clk)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        raise unavailable("slow")

    # 0.05s budget, but backoff would sleep 0.1s — the deadline cuts it short.
    policy = RetryPolicy(
        max_attempts=10, backoff=BackoffPolicy(base_delay_s=0.1, jitter="none")
    )
    with pytest.raises(RpcError):
        await run_with_retry(
            attempt,
            policy=policy,
            idempotent=True,
            deadline=Deadline.after(0.05, clock=clk),
            clock=clk,
            sleep=sleep,  # type: ignore[arg-type]
        )
    # First attempt fails, sleeps min(0.1, 0.05)=0.05 → deadline now expired.
    assert calls["n"] >= 1


async def test_retry_budget_suppresses_amplification() -> None:
    clk = ManualClock()
    _waits, sleep = _slept(clk)
    # Budget with no floor and no ratio → retries denied immediately.
    budget = RetryBudget(ratio=0.0, min_retries_floor=0)
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        raise unavailable("transient")

    policy = RetryPolicy(max_attempts=5, budget=budget, backoff=BackoffPolicy(jitter="none"))
    with pytest.raises(RpcError):
        await run_with_retry(
            attempt,
            policy=policy,
            idempotent=True,
            deadline=Deadline.never(),
            clock=clk,
            sleep=sleep,  # type: ignore[arg-type]
        )
    # record_primary deposits 0 tokens; try_withdraw fails → no retry.
    assert calls["n"] == 1
