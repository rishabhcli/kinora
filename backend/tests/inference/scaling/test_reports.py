"""Unit tests for capacity-planning reports (app.inference.scaling.reports)."""

from __future__ import annotations

from app.inference.scaling.reports import (
    CapacityPlanner,
    evaluate_fleet_slo,
    slo_set_for_latency_target,
)
from app.inference.scaling.workload import BurstLoad, ConstantLoad, RampLoad
from app.reliability.latency import LatencyDigest

# --------------------------------------------------------------------------- #
# SLO evaluation reuse
# --------------------------------------------------------------------------- #


def test_slo_set_has_p95_p99_and_attainment() -> None:
    s = slo_set_for_latency_target(p95_target_ms=1000.0, p99_target_ms=2000.0)
    kinds = {slo.kind.value for slo in s.slos}
    assert "latency_p95_ms" in kinds
    assert "latency_p99_ms" in kinds
    assert "availability" in kinds


def test_evaluate_fleet_slo_passes_when_within_targets() -> None:
    digest = LatencyDigest()
    digest.extend_ms([100.0] * 100)
    s = slo_set_for_latency_target(p95_target_ms=1000.0, p99_target_ms=2000.0)
    verdict = evaluate_fleet_slo(s, latency=digest.summary(), attainment=1.0)
    assert verdict.passed


def test_evaluate_fleet_slo_fails_on_latency_miss() -> None:
    digest = LatencyDigest()
    digest.extend_ms([5000.0] * 100)  # way over target
    s = slo_set_for_latency_target(p95_target_ms=1000.0, p99_target_ms=2000.0)
    verdict = evaluate_fleet_slo(s, latency=digest.summary(), attainment=1.0)
    assert not verdict.passed
    assert len(verdict.violations) >= 1


def test_evaluate_fleet_slo_fails_on_attainment_miss() -> None:
    digest = LatencyDigest()
    digest.extend_ms([100.0] * 100)
    s = slo_set_for_latency_target(
        p95_target_ms=1000.0, p99_target_ms=2000.0, attainment_target=0.99
    )
    verdict = evaluate_fleet_slo(s, latency=digest.summary(), attainment=0.5)
    assert not verdict.passed
    names = {v.slo.name for v in verdict.violations}
    assert "fleet-attainment" in names


# --------------------------------------------------------------------------- #
# Full plan assembly
# --------------------------------------------------------------------------- #


def test_plan_light_load_passes_and_recommends() -> None:
    planner = CapacityPlanner(
        profile=ConstantLoad(0.4), horizon_s=400.0, slo_target_s=60.0,
        instance_type="gpu-a10", seed=3,
    )
    report = planner.plan()
    assert report.passed
    assert report.sizing.servers >= 1
    assert report.simulation.completed > 0
    assert report.recommended_label is not None  # Pareto sweep ran by default


def test_plan_to_dict_and_text() -> None:
    planner = CapacityPlanner(
        profile=ConstantLoad(0.4), horizon_s=300.0, slo_target_s=60.0, seed=1,
    )
    report = planner.plan()
    payload = report.to_dict()
    assert str(payload["backend_id"]).startswith("plan::")
    assert "sizing" in payload and "simulation" in payload and "pareto" in payload
    text = report.render_text()
    assert "Capacity plan" in text
    assert "SLO target" in text


def test_plan_without_pareto_skips_sweep() -> None:
    planner = CapacityPlanner(
        profile=ConstantLoad(0.4), horizon_s=300.0, slo_target_s=60.0,
        run_pareto=False, seed=1,
    )
    report = planner.plan()
    assert report.frontier.frontier == ()
    assert report.recommended_label is None
    # The analytical sizing + validation sim still ran.
    assert report.sizing.servers >= 1
    assert report.simulation.arrivals > 0


def test_plan_is_deterministic() -> None:
    def run() -> dict[str, object]:
        planner = CapacityPlanner(
            profile=RampLoad(start_rate=0.2, end_rate=1.0, duration_s=300.0),
            horizon_s=300.0, slo_target_s=60.0, seed=9,
        )
        return planner.plan().to_dict()

    assert run() == run()


def test_plan_heavy_burst_records_defences() -> None:
    planner = CapacityPlanner(
        profile=BurstLoad(baseline_rate=0.3, spike_rate=5.0, center_s=120.0, width_s=15.0),
        horizon_s=400.0, slo_target_s=60.0, instance_type="gpu-a10", seed=2,
        committed_fraction=0.5,
    )
    report = planner.plan()
    sim = report.simulation
    # A burst exercises shedding and/or preemption.
    assert (sim.shed + sim.preemptions) > 0
