"""Tests for request hedging (tail-latency mitigation)."""

from __future__ import annotations

import pytest

from app.distributed.rpc.deadline import Deadline, ManualClock
from app.distributed.rpc.errors import RpcError, unavailable
from app.distributed.rpc.hedging import HedgeBudget, HedgePolicy, run_with_hedging


def _sleep_fn(clk: ManualClock) -> object:
    async def _sleep(s: float) -> None:
        if s > 0:
            clk.advance(s)

    return _sleep


async def test_disabled_policy_runs_single_attempt() -> None:
    clk = ManualClock()
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        return "primary"

    result = await run_with_hedging(
        attempt,
        policy=HedgePolicy(),  # disabled (max_hedges=0)
        idempotent=True,
        deadline=Deadline.never(),
        clock=clk,
        sleep=_sleep_fn(clk),  # type: ignore[arg-type]
    )
    assert result == "primary"
    assert calls["n"] == 1


async def test_non_idempotent_never_hedges() -> None:
    clk = ManualClock()
    calls = {"n": 0}

    async def attempt(_i: int) -> str:
        calls["n"] += 1
        return "x"

    result = await run_with_hedging(
        attempt,
        policy=HedgePolicy(delay_s=0.01, max_hedges=2),
        idempotent=False,
        deadline=Deadline.never(),
        clock=clk,
        sleep=_sleep_fn(clk),  # type: ignore[arg-type]
    )
    assert result == "x"
    assert calls["n"] == 1


async def test_fast_primary_wins_no_hedge_fired() -> None:
    clk = ManualClock()
    attempts: list[int] = []

    async def attempt(i: int) -> str:
        attempts.append(i)
        return f"won-{i}"

    result = await run_with_hedging(
        attempt,
        policy=HedgePolicy(delay_s=0.05, max_hedges=2),
        idempotent=True,
        deadline=Deadline.after(1.0, clock=clk),
        clock=clk,
        sleep=_sleep_fn(clk),  # type: ignore[arg-type]
    )
    # Primary returns immediately; by the time the hedge delay elapses there is a
    # winner, so no hedge leg launches.
    assert result == "won-0"
    assert attempts == [0]


async def test_all_legs_fail_raises_last() -> None:
    clk = ManualClock()

    async def attempt(_i: int) -> str:
        raise unavailable("down")

    with pytest.raises(RpcError):
        await run_with_hedging(
            attempt,
            policy=HedgePolicy(delay_s=0.01, max_hedges=2),
            idempotent=True,
            deadline=Deadline.after(1.0, clock=clk),
            clock=clk,
            sleep=_sleep_fn(clk),  # type: ignore[arg-type]
        )


async def test_hedge_budget_suppresses_hedges() -> None:
    clk = ManualClock()
    budget = HedgeBudget(ratio=0.0)  # empty → no hedge tokens
    attempts: list[int] = []

    async def attempt(i: int) -> str:
        attempts.append(i)
        raise unavailable("slow")  # primary fails so a hedge would be wanted

    with pytest.raises(RpcError):
        await run_with_hedging(
            attempt,
            policy=HedgePolicy(delay_s=0.01, max_hedges=3, budget=budget),
            idempotent=True,
            deadline=Deadline.after(1.0, clock=clk),
            clock=clk,
            sleep=_sleep_fn(clk),  # type: ignore[arg-type]
        )
    # Budget empty → only the primary leg ran.
    assert attempts == [0]


def test_hedge_budget_token_accounting() -> None:
    b = HedgeBudget(ratio=0.5, max_tokens=10)
    b.record_primary()
    b.record_primary()
    assert b.tokens == pytest.approx(1.0)
    assert b.try_withdraw()
    assert b.tokens == pytest.approx(0.0)
    assert not b.try_withdraw()
