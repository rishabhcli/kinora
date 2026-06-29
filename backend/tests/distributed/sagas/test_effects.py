"""Exactly-once effect ledger: dedup, concurrency, JSON discipline, stalled claims."""

from __future__ import annotations

import asyncio

import pytest

from app.distributed.sagas.effects import (
    EffectClaimStalled,
    EffectState,
    InMemoryEffectLedger,
)
from app.jobs.clock import ManualClock


async def test_once_runs_action_a_single_time() -> None:
    ledger = InMemoryEffectLedger(clock=ManualClock())
    calls = {"n": 0}

    async def action() -> str:
        calls["n"] += 1
        return "value"

    a = await ledger.once("k", action)
    b = await ledger.once("k", action)
    c = await ledger.once("k", action)
    assert a == b == c == "value"
    assert calls["n"] == 1
    assert "k" in ledger.applied_keys


async def test_once_supports_sync_and_async_actions() -> None:
    ledger = InMemoryEffectLedger()
    assert await ledger.once("sync", lambda: 7) == 7

    async def coro() -> int:
        return 9

    assert await ledger.once("async", coro) == 9


async def test_record_result_must_be_json_serialisable() -> None:
    ledger = InMemoryEffectLedger()

    async def bad() -> object:
        return object()  # not JSON-serialisable

    with pytest.raises(TypeError):
        await ledger.once("k", bad)


async def test_concurrent_callers_share_one_execution() -> None:
    """Two coroutines racing the same key both observe the single result.

    The loser of the claim waits for the winner's record rather than re-running
    the action — the exactly-once guarantee under concurrency.
    """
    ledger = InMemoryEffectLedger()
    calls = {"n": 0}
    gate = asyncio.Event()

    async def slow() -> str:
        calls["n"] += 1
        await gate.wait()  # hold the winner mid-flight
        return "once"

    winner = asyncio.create_task(ledger.once("k", slow))
    # Let the winner claim + start.
    for _ in range(5):
        await asyncio.sleep(0)
    loser = asyncio.create_task(ledger.once("k", slow))
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    assert await winner == "once"
    assert await loser == "once"
    assert calls["n"] == 1


async def test_claim_record_get_lifecycle() -> None:
    ledger = InMemoryEffectLedger()
    assert await ledger.get("k") is None
    assert await ledger.claim("k") is True
    assert await ledger.claim("k") is False  # second claim loses
    rec = await ledger.get("k")
    assert rec is not None and rec.state is EffectState.PENDING
    await ledger.record("k", result={"ok": True}, undo_token="undo-1")
    rec = await ledger.get("k")
    assert rec is not None and rec.state is EffectState.APPLIED
    assert rec.result == {"ok": True}
    assert rec.undo_token == "undo-1"


async def test_stalled_claim_surfaces_then_reaps() -> None:
    """A claimed-but-unrecorded key surfaces as stalled; the reaper clears it."""
    ledger = InMemoryEffectLedger()
    # Manually leave a stale PENDING claim (a crashed claimer).
    assert await ledger.claim("k") is True

    async def action() -> str:
        return "v"

    # A different caller now finds the key claimed (not by it) and never recorded.
    with pytest.raises(EffectClaimStalled):
        await ledger.once("k", action)

    cleared = await ledger.reap_stalled()
    assert cleared == 1
    # After reaping, the next once() can claim and run cleanly.
    assert await ledger.once("k", action) == "v"


async def test_action_exception_releases_claim_for_in_process_retry() -> None:
    """If the action raises, the claim is released so a same-process retry re-runs.

    A transient failure inside ``once`` did not apply the effect, so the next
    attempt must be free to re-claim and re-run — not be blocked as 'stalled'.
    This is what lets a saga step's retry re-execute its ledger-wrapped side effect.
    """
    ledger = InMemoryEffectLedger()
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    with pytest.raises(RuntimeError):
        await ledger.once("k", flaky)
    # The claim was released — no stalled record lingers.
    assert await ledger.get("k") is None
    # The retry re-claims and succeeds; the effect ran exactly twice total (one
    # failed attempt that applied nothing + one successful application).
    assert await ledger.once("k", flaky) == "ok"
    assert calls["n"] == 2
    assert "k" in ledger.applied_keys


async def test_undo_token_round_trips() -> None:
    ledger = InMemoryEffectLedger()
    await ledger.once("k", lambda: "result", undo_token={"reservation_id": "res_42"})
    rec = await ledger.get("k")
    assert rec is not None
    assert rec.undo_token == {"reservation_id": "res_42"}
