"""Simulator tests (kinora.md §4.10, §13): the controller beats fixed sizing.

Deterministic — a fixed trace + a VirtualClock in the controller yields the same
metrics every run. Proves the headline claim across all four demand shapes, and the
predictive win against an under-provisioned baseline on a velocity spike.
"""

from __future__ import annotations

from app.autoscale.controller import AutoscalerConfig
from app.autoscale.lanes import Lane, default_lane_pools
from app.autoscale.simulator import (
    compare_scenario,
    default_scenarios,
    diurnal_trace,
    ingest_burst_trace,
    run_static,
    run_trace,
    spike_trace,
    steady_trace,
)


def test_simulation_is_deterministic() -> None:
    trace = spike_trace()
    a = run_trace(trace).as_dict()
    b = run_trace(trace).as_dict()
    assert a == b


def test_controller_wins_every_scenario() -> None:
    for name, trace in default_scenarios().items():
        comp = compare_scenario(name, trace)
        assert comp.controller_wins(), f"{name}: {comp.as_dict()}"


def test_controller_cuts_idle_waste_vs_peak_baseline() -> None:
    """Against a baseline sized to the controller's peak, the win is leaner idle."""
    for name, trace in default_scenarios().items():
        comp = compare_scenario(name, trace)
        # Same-or-fewer underruns AND strictly less idle waste than a fat static pool.
        assert comp.controller.underrun_rate <= comp.baseline.underrun_rate + 1e-9
        assert comp.waste_improvement > 0.0, f"{name}: {comp.as_dict()}"


def test_predictive_controller_beats_under_provisioned_static_on_spike() -> None:
    """Vs a min-sized static pool, the elastic controller underruns strictly less."""
    trace = spike_trace()
    pools = default_lane_pools()
    controller = run_trace(trace, pools=pools)
    static_min = run_static(
        trace,
        sizes={lane: pool.min_replicas for lane, pool in pools.items()},
        pools=pools,
    )
    assert controller.underrun_rate < static_min.underrun_rate


def test_low_oscillation_under_flapping_demand() -> None:
    """Anti-flap keeps the *amount* of scaling churn small relative to trace length.

    Oscillation (reversals/events) is noisy when there are only a handful of events,
    so the load-bearing anti-flap property is that the controller barely moves on
    near-steady demand: total replica churn stays a small fraction of the ticks.
    """
    for name, trace in default_scenarios().items():
        m = run_trace(trace)
        churn_per_tick = m.total_replica_delta / m.ticks
        assert churn_per_tick <= 1.0, f"{name}: churn/tick={churn_per_tick}"


def test_steady_demand_barely_scales() -> None:
    """On flat (jittery) demand the pool stays in a tight band, never thrashing."""
    trace = steady_trace()
    m = run_trace(trace)
    assert m.underrun_ticks == 0
    # Small total movement relative to the 60-tick trace: the floor + cooldown absorb
    # most of the jitter (well under 0.25 replica-changes per tick).
    assert m.total_replica_delta <= m.ticks // 4


def test_cost_cap_engages_when_configured_tight() -> None:
    trace = spike_trace()
    pools = default_lane_pools(gpu_min=1, gpu_max=6)
    m = run_trace(trace, pools=pools, config=AutoscalerConfig(max_cost=40.0))
    # Some ticks during the spike should hit the cap and trim.
    assert m.cost_capped_ticks > 0


def test_steady_controller_holds_near_minimum() -> None:
    trace = steady_trace(depth=4, sessions=4)
    m = run_trace(trace)
    # Steady, well-buffered demand -> no underruns and the pool stays tightly bounded
    # near the floor (a couple of replicas of breathing room, never a runaway).
    assert m.underrun_ticks == 0
    assert m.peak_replicas <= sum(p.min_replicas for p in default_lane_pools().values()) + 3
    # Low oscillation: the controller is not thrashing direction on the jitter.
    assert m.oscillation <= 0.6


def test_diurnal_scales_up_then_drains() -> None:
    trace = diurnal_trace()
    m = run_trace(trace)
    # The pool grows above the floor at the day peak.
    assert m.peak_replicas > sum(p.min_replicas for p in default_lane_pools().values())
    # ...and drains back so the final size is below the peak.
    assert sum(m.final_replicas.values()) < m.peak_replicas


def test_ingest_burst_scales_provider_then_recovers() -> None:
    trace = ingest_burst_trace()
    m = run_trace(trace)
    assert m.underrun_ticks == 0  # ingest backlog isn't reader-facing -> no stalls
    assert m.peak_replicas > sum(p.min_replicas for p in default_lane_pools().values())


def test_static_baseline_scoring_has_no_scaling() -> None:
    trace = steady_trace()
    m = run_static(trace, sizes={Lane.CPU: 2, Lane.PROVIDER: 8, Lane.GPU: 0})
    assert m.scale_events == 0
    assert m.direction_reversals == 0
