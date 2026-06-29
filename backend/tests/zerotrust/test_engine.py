"""Unit tests for the threat-detection engine wiring (defense.engine)."""

from __future__ import annotations

from app.zerotrust.defense.clock import ManualClock
from app.zerotrust.defense.detectors.rate import RateAnomalyDetector, RateConfig
from app.zerotrust.defense.engine import ThreatEngine
from app.zerotrust.defense.store import InMemoryAlertStore

from .traces import brute_force, merge, noisy_baseline


def _engine(clk: ManualClock, floor: int = 20) -> tuple[ThreatEngine, InMemoryAlertStore]:
    store = InMemoryAlertStore()
    det = RateAnomalyDetector(config=RateConfig(window=60.0, absolute_floor=floor), clock=clk)
    eng = ThreatEngine([det], sink=store, clock=clk, sweep_interval=30.0)
    return eng, store


def test_engine_detects_brute_force_in_trace() -> None:
    clk = ManualClock()
    eng, store = _engine(clk, floor=20)
    trace = brute_force(start=clk.wall(), n=50, spacing=0.2)
    for ev in trace:
        clk.at(ev.ts)
        eng.ingest(ev)
    assert len(store) >= 1
    assert eng.stats.events == 50
    assert eng.stats.emitted_alerts >= 1
    # The flood collapses into a small number of rolled-up alerts, not 30 rows.
    assert eng.stats.emitted_alerts < eng.stats.raw_alerts


def test_engine_quiet_on_benign_baseline() -> None:
    clk = ManualClock()
    eng, store = _engine(clk, floor=20)
    trace = noisy_baseline(start=clk.wall(), n=200, seed=3)
    for ev in trace:
        clk.at(ev.ts)
        eng.ingest(ev)
    # No single ip in the benign baseline should trip a floor of 20/60s.
    assert len(store) == 0


def test_engine_register_is_additive() -> None:
    clk = ManualClock()
    eng = ThreatEngine([], clock=clk)
    assert eng.detectors == []
    det = RateAnomalyDetector(config=RateConfig(absolute_floor=5), clock=clk)
    eng.register(det)
    assert det in eng.detectors


def test_engine_periodic_sweep_runs() -> None:
    clk = ManualClock()
    eng, _ = _engine(clk, floor=5)
    trace = merge(brute_force(start=0.0, n=5, spacing=0.1))
    for ev in trace:
        clk.at(ev.ts)
        eng.ingest(ev)
    clk.advance(120.0)
    eng.force_sweep()
    assert eng.stats.sweeps >= 1


def test_engine_stats_serialisable() -> None:
    clk = ManualClock()
    eng, _ = _engine(clk)
    d = eng.stats.as_dict()
    assert set(d) == {
        "events",
        "raw_alerts",
        "emitted_alerts",
        "suppressed_alerts",
        "sweeps",
        "by_detector",
    }
