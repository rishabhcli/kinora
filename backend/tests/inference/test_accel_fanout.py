"""Fan-out racing tests — first-good-wins, cost caps, hedging, validation.

Determinism comes from gated backends (the test releases whichever candidate
should win) and a ManualClock whose hedge ``sleep`` only resolves on a winner
signal — no wall-clock timing anywhere.
"""

from __future__ import annotations

import asyncio

import pytest

from app.inference.accel.errors import CostCapExceededError, FanOutExhaustedError
from app.inference.accel.fakes import GatedBackend, ManualClock, StaticBackend
from app.inference.accel.fanout import (
    FanOutRacer,
    ProviderCandidate,
    first_good,
)
from app.inference.accel.metrics import FanOutMetrics
from app.inference.accel.protocol import GenerationRequest, GenerationResult

REQ = GenerationRequest.from_prompt("race me")


def _cand(name: str, backend: object, *, cost: float = 1.0, priority: int = 0) -> ProviderCandidate:
    return ProviderCandidate(name=name, generate=backend.generate, cost=cost, priority=priority)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Basic racing
# --------------------------------------------------------------------------- #


async def test_single_candidate_wins() -> None:
    racer = FanOutRacer(metrics=FanOutMetrics())
    out = await racer.race(REQ, [_cand("solo", StaticBackend("hello world"))])
    assert out.winner == "solo"
    assert out.result.text == "hello world"
    assert out.result.meta["provider"] == "solo"
    assert out.cost_spent == 1.0


async def test_primary_only_started_when_hedge_blocks() -> None:
    # ManualClock.sleep never times out on its own; the primary answers
    # immediately (StaticBackend), so the secondary is never started.
    primary = StaticBackend("primary answer", model="p")
    secondary = GatedBackend("secondary answer", model="s")
    racer = FanOutRacer(hedge_delay=5.0, clock=ManualClock(), metrics=FanOutMetrics())
    out = await racer.race(
        REQ,
        [_cand("primary", primary, priority=0), _cand("secondary", secondary, priority=1)],
    )
    assert out.winner == "primary"
    assert out.started == ["primary"]  # secondary never launched
    assert secondary.calls == 0
    assert out.cost_spent == 1.0


async def test_hedge_falls_through_to_secondary_when_primary_hangs() -> None:
    primary = GatedBackend("slow primary", model="p")  # never released -> hangs
    secondary = StaticBackend("fast secondary", model="s")
    racer = FanOutRacer(hedge_delay=0.0, clock=ManualClock(), metrics=FanOutMetrics())
    # hedge_delay 0 -> both launched; the static secondary wins immediately.
    out = await racer.race(
        REQ,
        [_cand("primary", primary, priority=0), _cand("secondary", secondary, priority=1)],
    )
    assert out.winner == "secondary"
    assert "primary" in out.started and "secondary" in out.started
    assert out.losers_cancelled == 1  # the hung primary was cancelled


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_invalid_answer_is_not_a_win() -> None:
    bad = StaticBackend("oops", model="b")
    good = StaticBackend("the correct answer", model="g")

    def validate(r: GenerationResult) -> bool:
        return "correct" in r.text

    racer = FanOutRacer(hedge_delay=0.0, validate=validate, clock=ManualClock())
    out = await racer.race(
        REQ, [_cand("bad", bad, priority=0), _cand("good", good, priority=1)]
    )
    assert out.winner == "good"


async def test_all_invalid_raises_exhausted() -> None:
    a = StaticBackend("nope", model="a")
    b = StaticBackend("also nope", model="b")
    racer = FanOutRacer(hedge_delay=0.0, validate=lambda r: False, clock=ManualClock())
    with pytest.raises(FanOutExhaustedError) as ei:
        await racer.race(REQ, [_cand("a", a, priority=0), _cand("b", b, priority=1)])
    assert ei.value.attempts == 2


async def test_all_fail_raises_with_last_error() -> None:
    boom = RuntimeError("provider down")
    a = StaticBackend("x", fail_with=boom)
    b = StaticBackend("y", fail_with=ValueError("also down"))
    racer = FanOutRacer(hedge_delay=0.0, clock=ManualClock())
    with pytest.raises(FanOutExhaustedError) as ei:
        await racer.race(REQ, [_cand("a", a, priority=0), _cand("b", b, priority=1)])
    assert ei.value.attempts == 2
    assert isinstance(ei.value.last_error, (RuntimeError, ValueError))


async def test_one_fails_other_wins() -> None:
    failing = StaticBackend("x", fail_with=RuntimeError("down"))
    ok = StaticBackend("recovered", model="ok")
    racer = FanOutRacer(hedge_delay=0.0, clock=ManualClock(), metrics=FanOutMetrics())
    out = await racer.race(
        REQ, [_cand("failing", failing, priority=0), _cand("ok", ok, priority=1)]
    )
    assert out.winner == "ok"


# --------------------------------------------------------------------------- #
# Cost caps
# --------------------------------------------------------------------------- #


async def test_cost_cap_limits_started_candidates() -> None:
    # cap 2.5, each candidate costs 1.0 -> only two may start; both gated + hung.
    g1 = GatedBackend("a")
    g2 = GatedBackend("b")
    g3 = GatedBackend("c")
    metrics = FanOutMetrics()
    racer = FanOutRacer(cost_cap=2.5, hedge_delay=0.0, clock=ManualClock(), metrics=metrics)

    async def drive() -> None:
        # let the racer launch the (capped) candidates, then release one winner
        await asyncio.sleep(0)
        for _ in range(50):
            if g1.started.is_set() and g2.started.is_set():
                break
            await asyncio.sleep(0)
        g1.release()

    task = asyncio.ensure_future(racer.race(REQ, [
        _cand("g1", g1, cost=1.0, priority=0),
        _cand("g2", g2, cost=1.0, priority=1),
        _cand("g3", g3, cost=1.0, priority=2),
    ]))
    await drive()
    out = await task
    assert out.winner == "g1"
    # Only two started (cap 2.5 < 3.0); g3 never launched.
    assert set(out.started) == {"g1", "g2"}
    assert g3.calls == 0
    assert out.cost_spent == 2.0


async def test_cost_cap_rejects_when_cheapest_too_expensive() -> None:
    metrics = FanOutMetrics()
    racer = FanOutRacer(cost_cap=1.0, clock=ManualClock(), metrics=metrics)
    with pytest.raises(CostCapExceededError) as ei:
        await racer.race(REQ, [_cand("pricey", StaticBackend("x"), cost=5.0)])
    assert ei.value.cost_cap == 1.0
    assert ei.value.would_spend == 5.0
    assert metrics.snapshot().cap_rejections == 1


async def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        FanOutRacer(cost_cap=0.0)


async def test_no_candidates_raises() -> None:
    racer = FanOutRacer()
    with pytest.raises(FanOutExhaustedError):
        await racer.race(REQ, [])


# --------------------------------------------------------------------------- #
# Priority + metrics + convenience
# --------------------------------------------------------------------------- #


async def test_priority_orders_launch() -> None:
    # Lower priority value launches first; with hedge_delay 0 both launch but
    # the test asserts the winner attribution and start order is recorded.
    a = StaticBackend("from a", model="a")
    b = StaticBackend("from b", model="b")
    racer = FanOutRacer(hedge_delay=0.0, clock=ManualClock())
    out = await racer.race(
        REQ, [_cand("b", b, priority=5), _cand("a", a, priority=1)]
    )
    # 'a' has lower priority value -> first in the started order.
    assert out.started[0] == "a"


async def test_metrics_accumulate_across_races() -> None:
    metrics = FanOutMetrics()
    racer = FanOutRacer(hedge_delay=0.0, clock=ManualClock(), metrics=metrics)
    await racer.race(REQ, [_cand("x", StaticBackend("ok"))])
    await racer.race(REQ, [_cand("y", StaticBackend("ok2"))])
    snap = metrics.snapshot()
    assert snap.races == 2
    assert snap.wins == 2
    assert snap.cost_charged == 2.0


async def test_first_good_helper() -> None:
    out = await first_good(
        REQ, [ProviderCandidate("only", StaticBackend("done").generate)], clock=ManualClock()
    )
    assert out.winner == "only"
    assert out.result.text == "done"
