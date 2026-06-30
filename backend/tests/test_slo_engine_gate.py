"""Deterministic tests for the SLO engine + release-gate (synthetic streams)."""

from __future__ import annotations

import pytest

from app.slo.engine import SLOEngine, build_default_engine
from app.slo.gate import GateConfig, GateDecision, decide_gate
from app.slo.objectives import AlertSeverity, LatencyObjective, Objective
from app.slo.sli import DEFAULT_SLIS, SLIDefinition, SLIType


def _by_name() -> dict[str, SLIDefinition]:
    return {d.name: d for d in DEFAULT_SLIS}


def _feed_reads(engine: SLOEngine, *, good: int, bad: int, now: float) -> None:
    if good:
        engine.record_event("read.underrun_free", good=True, now=now, weight=good)
    if bad:
        engine.record_event("read.underrun_free", good=False, now=now, weight=bad)


# --- engine SLI/budget snapshot ------------------------------------------- #


def test_engine_status_healthy_when_all_objectives_met() -> None:
    engine = build_default_engine()
    now = 1000.0
    # 999 good / 1 bad reads => 0.999 > 0.99 target.
    engine.record_event("read.underrun_free", good=True, now=now, weight=999)
    engine.record_event("read.underrun_free", good=False, now=now, weight=1)
    engine.record_event("shot.success", good=True, now=now, weight=100)
    engine.record_event("api.availability", good=True, now=now, weight=1000)
    engine.record_sample("render.latency_ms", 4000.0, now=now)
    engine.record_sample("api.intent_latency_ms", 100.0, now=now)

    status = engine.status(now=now)
    assert status.healthy is True
    assert status.gate.decision is GateDecision.ALLOW
    budgets = {b.objective.name: b for b in status.budgets}
    assert budgets["read-underrun-free"].met is True
    assert budgets["read-underrun-free"].good_ratio == pytest.approx(0.999)


def test_engine_empty_streams_are_vacuously_healthy() -> None:
    engine = build_default_engine()
    status = engine.status(now=10.0)
    assert status.healthy is True
    assert status.gate.decision is GateDecision.ALLOW
    # No traffic => full budget, no burn, latency objectives vacuously met.
    assert all(b.remaining_fraction == pytest.approx(1.0) for b in status.budgets)
    assert all(not a.firing for a in status.alerts)


def test_engine_latency_miss_makes_status_unhealthy() -> None:
    engine = build_default_engine(render_p95_ms=5000.0)
    now = 100.0
    for _ in range(20):
        engine.record_sample("render.latency_ms", 9000.0, now=now)  # over budget
    status = engine.status(now=now)
    assert status.healthy is False
    render = next(v for v in status.latency if v.objective.name == "render-p95")
    assert render.met is False


def test_engine_fast_burn_pages_and_freezes_gate() -> None:
    # availability target 0.995 => budget 0.005. A sustained 0.072 failure rate
    # across the 1h + 5m windows = 14.4x burn => PAGE => gate FREEZE.
    engine = build_default_engine(availability_target=0.995)
    now = 10_000.0
    # Feed ~7.2% failures spread across the last hour so both windows see it.
    for t in range(0, 3600, 5):
        ts = now - 3600 + t
        engine.record_event("api.availability", good=True, now=ts, weight=928)
        engine.record_event("api.availability", good=False, now=ts, weight=72)
    status = engine.status(now=now)
    avail_alert = next(a for a in status.alerts if a.objective_name == "api-availability")
    assert avail_alert.severity is AlertSeverity.PAGE
    assert status.gate.decision is GateDecision.FREEZE
    assert status.gate.can_release is False
    assert status.gate.can_promote_canary is False


# --- gate decision (pure) -------------------------------------------------- #


def _engine_with_budget(good_ratio: float, target: float = 0.99) -> SLOEngine:
    """Build a minimal engine whose single objective sits at ``good_ratio``."""
    engine = SLOEngine(eval_window_s=300.0)
    sli = _by_name()["api_availability"]
    engine.register_objective(Objective("api-availability", sli, target))
    total = 10_000
    bad = round(total * (1.0 - good_ratio))
    good = total - bad
    now = 50_000.0
    # Spread evenly so the long budget window captures all of it.
    if good:
        engine.record_event(sli.stream, good=True, now=now, weight=good)
    if bad:
        engine.record_event(sli.stream, good=False, now=now, weight=bad)
    return engine


def test_gate_allow_when_budget_full() -> None:
    engine = _engine_with_budget(1.0)
    gate = engine.release_gate(now=50_000.0)
    assert gate.decision is GateDecision.ALLOW
    assert gate.can_release is True
    assert gate.can_promote_canary is True


def test_gate_caution_when_budget_low() -> None:
    # target 0.99 => budget 0.01; failure 0.009 => 90% consumed => 10% remaining,
    # below the 25% caution floor but not exhausted => CAUTION (release ok, no canary).
    engine = _engine_with_budget(0.991, target=0.99)
    status = engine.status(now=50_000.0)
    gate = status.gate
    assert gate.decision is GateDecision.CAUTION
    assert gate.can_release is True
    assert gate.can_promote_canary is False


def test_gate_freeze_when_budget_exhausted() -> None:
    engine = _engine_with_budget(0.97, target=0.99)  # failure 0.03 > budget 0.01
    gate = engine.release_gate(now=50_000.0)
    assert gate.decision is GateDecision.FREEZE
    assert gate.can_release is False


def test_decide_gate_is_pure_over_status() -> None:
    engine = _engine_with_budget(1.0)
    status = engine.status(now=50_000.0)
    # Re-deciding with a stricter caution floor flips ALLOW->CAUTION deterministically.
    strict = decide_gate(status, config=GateConfig(caution_floor=1.01))
    assert strict.decision is GateDecision.CAUTION


def test_engine_register_replaces_objective_and_creates_stream() -> None:
    engine = SLOEngine()
    sli = _by_name()["api_availability"]
    engine.register_objective(Objective("a", sli, 0.99))
    engine.register_objective(Objective("a", sli, 0.999))  # replace
    assert len(engine.objectives) == 1
    assert engine.objectives[0].target == 0.999


def test_latency_objective_requires_latency_sli() -> None:
    ratio = _by_name()["api_availability"]
    with pytest.raises(ValueError):
        LatencyObjective("bad", ratio, target_ms=10.0)
    # And the ratio Objective rejects a latency SLI.
    lat = _by_name()["render_latency_p95"]
    assert lat.type is SLIType.LATENCY_P95
    with pytest.raises(ValueError):
        Objective("bad2", lat, 0.99)
