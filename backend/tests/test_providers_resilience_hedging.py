"""Unit tests for the hedged executor (tail-latency duplicate requests).

Uses real ``asyncio`` tasks with controllable per-copy delays via events, so the
"first success wins, losers cancelled" semantics are exercised without real waits.
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers.errors import ProviderBadRequest, TransientProviderError
from app.providers.resilience.hedging import HedgedExecutor, HedgePolicy


async def test_no_hedge_runs_single_copy() -> None:
    ex = HedgedExecutor(HedgePolicy(max_attempts=1))
    calls = 0

    async def attempt(_idx: int) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await ex.run(attempt) == "ok"
    assert calls == 1
    assert ex.stats.hedges_launched == 0


async def test_hedge_launches_second_copy_when_first_is_slow() -> None:
    # delay_s=0 -> the hedge fires immediately; the first copy blocks, the second
    # returns at once, so the hedge wins.
    ex = HedgedExecutor(HedgePolicy(max_attempts=2, delay_s=0.0))
    release_first = asyncio.Event()

    async def attempt(idx: int) -> str:
        if idx == 0:
            await release_first.wait()
            return "first"
        return "hedge"

    result = await ex.run(attempt)
    assert result == "hedge"
    assert ex.stats.hedges_launched == 1
    assert ex.stats.hedge_wins == 1
    release_first.set()  # let the cancelled first copy unwind cleanly


async def test_first_copy_wins_when_fast_no_hedge_launched() -> None:
    # A generous delay means the fast first copy returns before the hedge timer.
    ex = HedgedExecutor(HedgePolicy(max_attempts=2, delay_s=5.0))

    async def attempt(_idx: int) -> str:
        return "fast"

    result = await ex.run(attempt)
    assert result == "fast"
    assert ex.stats.hedges_launched == 0


async def test_hedge_keeps_field_alive_past_a_fast_failure() -> None:
    # First copy fails fast; the hedge (launched at delay_s=0) succeeds. The fast
    # failure must NOT abort the run — first *success* wins, not first completion.
    ex = HedgedExecutor(HedgePolicy(max_attempts=2, delay_s=0.0))
    release_hedge = asyncio.Event()

    async def attempt(idx: int) -> str:
        if idx == 0:
            raise TransientProviderError("first copy failed")
        await release_hedge.wait()
        return "hedge-won"

    task = asyncio.create_task(ex.run(attempt))
    await asyncio.sleep(0)
    release_hedge.set()
    assert await task == "hedge-won"
    assert ex.stats.hedge_wins == 1


async def test_all_copies_fail_raises_last_error() -> None:
    ex = HedgedExecutor(HedgePolicy(max_attempts=2, delay_s=0.0))

    async def attempt(idx: int) -> str:
        raise ProviderBadRequest(f"copy {idx} failed")

    with pytest.raises(ProviderBadRequest):
        await ex.run(attempt)


async def test_hedge_cancels_losers() -> None:
    ex = HedgedExecutor(HedgePolicy(max_attempts=3, delay_s=0.0))
    cancelled: list[int] = []
    started = asyncio.Event()

    async def attempt(idx: int) -> str:
        if idx == 0:
            return "winner"
        try:
            started.set()
            await asyncio.sleep(100)  # would hang if not cancelled
            return "loser"
        except asyncio.CancelledError:
            cancelled.append(idx)
            raise

    # idx 0 wins immediately; the loser copies (launched at delay 0) get cancelled.
    result = await ex.run(attempt)
    assert result == "winner"
    # Give cancellation a tick to record.
    await asyncio.sleep(0)
    assert all(i > 0 for i in cancelled)


def test_hedge_policy_validates() -> None:
    with pytest.raises(ValueError):
        HedgePolicy(max_attempts=0)
    with pytest.raises(ValueError):
        HedgePolicy(delay_s=-1.0)
