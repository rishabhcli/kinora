"""Tests for SLO evaluation + breach detection (offline)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.models import SLOTarget
from deploy.orchestrator.slo import DEFAULT_RENDER_SLOS, SLOEvaluator


def _ev(*targets: SLOTarget) -> SLOEvaluator:
    return SLOEvaluator(list(targets))


def test_requires_targets() -> None:
    with pytest.raises(ValueError):
        SLOEvaluator([])


def test_rejects_duplicate_target_names() -> None:
    with pytest.raises(ValueError):
        SLOEvaluator([SLOTarget("x", 1.0), SLOTarget("x", 2.0)])


def test_single_breach_with_tolerance_one() -> None:
    ev = _ev(SLOTarget("error_rate", 0.05, higher_is_better=False))
    ev.observe({"error_rate": 0.5})
    assert ev.breached is True
    assert ev.result_for(ev.targets[0]).worst_value == 0.5


def test_breach_tolerance_requires_consecutive() -> None:
    ev = _ev(SLOTarget("error_rate", 0.05, higher_is_better=False, breach_tolerance=3))
    ev.observe({"error_rate": 0.5})  # breach 1
    ev.observe({"error_rate": 0.01})  # ok → resets
    ev.observe({"error_rate": 0.5})  # breach 1 again
    ev.observe({"error_rate": 0.5})  # breach 2
    assert ev.breached is False
    ev.observe({"error_rate": 0.5})  # breach 3 → tripped
    assert ev.breached is True


def test_worst_value_tracks_direction() -> None:
    higher = _ev(SLOTarget("success", 0.95, higher_is_better=True))
    higher.observe({"success": 0.99})
    higher.observe({"success": 0.80})
    higher.observe({"success": 0.97})
    # Worst for higher-is-better is the lowest seen.
    assert higher.result_for(higher.targets[0]).worst_value == 0.80

    lower = _ev(SLOTarget("latency", 100.0, higher_is_better=False))
    lower.observe({"latency": 50.0})
    lower.observe({"latency": 200.0})
    lower.observe({"latency": 80.0})
    # Worst for lower-is-better is the highest seen.
    assert lower.result_for(lower.targets[0]).worst_value == 200.0


def test_missing_metric_is_not_a_breach_and_resets_run() -> None:
    ev = _ev(SLOTarget("error_rate", 0.05, higher_is_better=False, breach_tolerance=2))
    ev.observe({"error_rate": 0.5})  # breach run = 1
    ev.observe({})  # missing → resets run
    ev.observe({"error_rate": 0.5})  # breach run = 1 again
    assert ev.breached is False


def test_multiple_targets_independent() -> None:
    ev = _ev(
        SLOTarget("success", 0.95, higher_is_better=True),
        SLOTarget("error_rate", 0.05, higher_is_better=False),
    )
    ev.observe({"success": 0.99, "error_rate": 0.5})
    breaches = ev.breaches()
    assert len(breaches) == 1
    assert breaches[0].target.name == "error_rate"


def test_reset_clears_state() -> None:
    ev = _ev(SLOTarget("error_rate", 0.05, higher_is_better=False))
    ev.observe({"error_rate": 0.5})
    assert ev.breached
    ev.reset()
    assert ev.samples_seen == 0
    assert not ev.breached


def test_default_render_slos_pass_on_healthy_sample() -> None:
    ev = SLOEvaluator(DEFAULT_RENDER_SLOS)
    for _ in range(5):
        ev.observe(
            {
                "render_success_ratio": 0.99,
                "error_rate": 0.01,
                "render_p99_latency_ms": 40_000.0,
                "queue_depth_growth": -1.0,
            }
        )
    assert ev.breached is False
    assert ev.samples_seen == 5


def test_default_render_slos_catch_low_success_ratio() -> None:
    ev = SLOEvaluator(DEFAULT_RENDER_SLOS)
    ev.observe({"render_success_ratio": 0.50, "error_rate": 0.01})
    assert ev.breached is True
    names = {r.target.name for r in ev.breaches()}
    assert "render_success_ratio" in names
