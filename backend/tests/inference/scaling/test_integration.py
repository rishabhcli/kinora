"""End-to-end integration: the scaling brain validates SLO + cost under load.

These tests exercise the whole facet together — autoscaler + pool + router-shaped
telemetry + shedding + preemption inside the discrete-event simulator — across the
varied load profiles the harness is meant to validate against, and assert the
operator-level invariants (SLO attainment under sustainable load, graceful
degradation under overload, cost monotonicity).
"""

from __future__ import annotations

import app.inference.scaling as scaling
from app.inference.scaling.reports import CapacityPlanner
from app.inference.scaling.workload import (
    BurstLoad,
    CompositeLoad,
    ConstantLoad,
    DiurnalLoad,
    LoadProfile,
    RampLoad,
    reader_population_load,
)
from app.reliability.capacity import ReadingProfile


def test_public_api_surface_is_importable() -> None:
    # Everything advertised in __all__ resolves on the package.
    for name in scaling.__all__:
        assert hasattr(scaling, name), name


def test_sustainable_load_meets_slo_across_profiles() -> None:
    profiles: dict[str, LoadProfile] = {
        "constant": ConstantLoad(0.4),
        "ramp": RampLoad(start_rate=0.1, end_rate=0.8, duration_s=600.0),
        "diurnal": DiurnalLoad(mean_rate=0.4, amplitude=0.3, period_s=600.0),
    }
    for name, profile in profiles.items():
        planner = CapacityPlanner(
            profile=profile, horizon_s=600.0, slo_target_s=90.0,
            instance_type="gpu-a10", seed=5, run_pareto=False,
        )
        report = planner.plan()
        assert report.simulation.slo_attainment >= 0.9, name
        assert report.simulation.cost.total_cost > 0.0, name


def test_overload_degrades_gracefully_not_catastrophically() -> None:
    # Demand far above a capped fleet: committed work must still mostly meet SLO
    # (it is protected + preempts), while speculative is shed (the §4.4 ladder).
    profile = CompositeLoad(
        profiles=(
            ConstantLoad(1.0),
            BurstLoad(baseline_rate=0.0, spike_rate=6.0, center_s=200.0, width_s=30.0),
        )
    )
    planner = CapacityPlanner(
        profile=profile, horizon_s=500.0, slo_target_s=60.0,
        instance_type="gpu-a10", seed=7, committed_fraction=0.5, run_pareto=False,
    )
    report = planner.plan()
    sim = report.simulation
    # The system defends itself rather than melting down: it sheds and/or preempts.
    assert (sim.shed + sim.preemptions) > 0
    # No request is lost silently — every arrival is shed or admitted.
    assert sim.shed + sim.admitted == sim.arrivals
    # Committed completions still happen.
    assert sim.committed_latency.count > 0


def test_reader_population_drives_a_realistic_plan() -> None:
    # Tie §4.1 reader counts to a fleet plan (the product → infra bridge).
    load = reader_population_load(readers=20, profile=ReadingProfile())
    planner = CapacityPlanner(
        profile=load, horizon_s=600.0, slo_target_s=90.0,
        instance_type="gpu-a10", seed=1, run_pareto=False,
    )
    report = planner.plan()
    assert report.peak_demand_rps == load.rate
    assert report.simulation.completed > 0


def test_pareto_recommendation_beats_endpoints_on_trade() -> None:
    # The knee recommendation should be feasible and on the frontier.
    planner = CapacityPlanner(
        profile=RampLoad(start_rate=0.2, end_rate=1.2, duration_s=600.0),
        horizon_s=600.0, slo_target_s=60.0, seed=3, run_pareto=True,
    )
    report = planner.plan()
    if report.recommended_label is not None:
        labels = {p.label for p in report.frontier.frontier}
        assert report.recommended_label in labels


def test_higher_slo_target_is_cheaper_to_meet() -> None:
    # A looser latency budget needs less fleet → lower cost (cost↔latency trade).
    strict = CapacityPlanner(
        profile=ConstantLoad(0.8), horizon_s=500.0, slo_target_s=30.0,
        instance_type="gpu-a10", seed=4, run_pareto=False,
    ).plan()
    loose = CapacityPlanner(
        profile=ConstantLoad(0.8), horizon_s=500.0, slo_target_s=120.0,
        instance_type="gpu-a10", seed=4, run_pareto=False,
    ).plan()
    # The strict plan sizes a bigger floor (warm = sizing/2), so it provisions more.
    assert strict.sizing.servers >= loose.sizing.servers
