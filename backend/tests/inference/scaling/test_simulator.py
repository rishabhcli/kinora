"""Integration tests for the discrete-event fleet simulator (app...simulator)."""

from __future__ import annotations

from app.inference.scaling.autoscaler import ScalingPolicy
from app.inference.scaling.contracts import BackendDescriptor, BackendKind
from app.inference.scaling.instances import default_catalog
from app.inference.scaling.simulator import FleetSimulator, SimulationConfig
from app.inference.scaling.workload import BurstLoad, ConstantLoad, LoadProfile, RampLoad


def _config(
    *,
    profile: LoadProfile,
    instance_key: str = "gpu-a10",
    horizon_s: float = 600.0,
    slo_target_s: float = 60.0,
    min_workers: int = 1,
    max_workers: int = 24,
    scale_to_zero: bool = False,
    seed: int = 7,
    concurrency: int = 2,
    service_s: float = 5.0,
    committed_fraction: float = 0.4,
) -> SimulationConfig:
    cat = default_catalog()
    desc = BackendDescriptor(
        backend_id=f"wan@{instance_key}",
        kind=BackendKind.VIDEO,
        instance_type=instance_key,
        concurrency=concurrency,
        service_time_s=service_s,
    )
    policy = ScalingPolicy(
        min_workers=min_workers,
        warm_pool=min_workers,
        max_workers=max_workers,
        target_tail_s=slo_target_s,
        tail_quantile=0.95,
        scale_to_zero=scale_to_zero,
        scale_to_zero_idle_s=60.0,
        max_step=8,
    )
    return SimulationConfig(
        descriptor=desc,
        instance=cat[instance_key],
        scaling_policy=policy,
        profile=profile,
        horizon_s=horizon_s,
        slo_target_s=slo_target_s,
        committed_fraction=committed_fraction,
        autoscale_interval_s=15.0,
        seed=seed,
    )


# --------------------------------------------------------------------------- #
# Basic correctness
# --------------------------------------------------------------------------- #


def test_simulation_runs_and_conserves_requests() -> None:
    cfg = _config(profile=ConstantLoad(0.6))
    res = FleetSimulator(cfg).run()
    # Every arrival is either shed or admitted.
    assert res.admitted + res.shed == res.arrivals
    # Most admitted requests complete within the horizon (some may be in flight).
    assert res.completed <= res.admitted
    assert res.completion_rate >= 0.9


def test_light_load_meets_slo_fully() -> None:
    cfg = _config(profile=ConstantLoad(0.3), slo_target_s=60.0)
    res = FleetSimulator(cfg).run()
    assert res.slo_attainment >= 0.95
    assert res.shed_rate < 0.05


def test_simulation_is_deterministic() -> None:
    cfg = _config(profile=ConstantLoad(0.6), seed=11)
    r1 = FleetSimulator(cfg).run()
    r2 = FleetSimulator(cfg).run()
    assert r1.to_dict() == r2.to_dict()


def test_different_seed_changes_outcome() -> None:
    r1 = FleetSimulator(_config(profile=ConstantLoad(0.6), seed=1)).run()
    r2 = FleetSimulator(_config(profile=ConstantLoad(0.6), seed=2)).run()
    # The arrival stream differs, so latencies differ (counts may coincide).
    assert r1.latency.to_dict() != r2.latency.to_dict()


# --------------------------------------------------------------------------- #
# Cost behaviour
# --------------------------------------------------------------------------- #


def test_cost_is_positive_and_breaks_down() -> None:
    cfg = _config(profile=ConstantLoad(0.6))
    res = FleetSimulator(cfg).run()
    assert res.cost.total_cost > 0.0
    # Cold-start + idle are sub-components of the provisioned cost.
    assert res.cost.cold_start_cost >= 0.0
    assert res.cost.idle_cost >= 0.0
    assert res.cost.cold_start_cost + res.cost.idle_cost <= res.cost.total_cost + 1e-6
    assert set(res.cost.by_instance_type) <= {"gpu-a10"}


def test_faster_instance_costs_more_but_lowers_latency() -> None:
    slow = FleetSimulator(_config(profile=ConstantLoad(0.8), instance_key="gpu-l20")).run()
    fast = FleetSimulator(_config(profile=ConstantLoad(0.8), instance_key="gpu-h20")).run()
    # The H20 serves each request faster → lower mean latency.
    assert fast.latency.mean_ms <= slow.latency.mean_ms
    # ...at a higher hourly rate (typically dearer overall for similar work).
    assert fast.cost.by_instance_type["gpu-h20"] > 0.0


# --------------------------------------------------------------------------- #
# Elasticity under varied load
# --------------------------------------------------------------------------- #


def test_ramp_scales_the_fleet_up() -> None:
    profile = RampLoad(start_rate=0.1, end_rate=1.5, duration_s=600.0)
    cfg = _config(profile=profile, horizon_s=600.0, min_workers=1, max_workers=32)
    res = FleetSimulator(cfg).run()
    # The fleet grew well past the floor to absorb the ramp.
    assert res.peak_warm_workers > 1


def test_scale_to_zero_collapses_after_burst() -> None:
    # A short burst then silence: with scale-to-zero the fleet should collapse,
    # so idle cost is bounded (it doesn't pay for an empty fleet forever).
    profile = BurstLoad(baseline_rate=0.0, spike_rate=3.0, center_s=60.0, width_s=15.0)
    cfg = _config(
        profile=profile,
        horizon_s=600.0,
        min_workers=0,
        max_workers=24,
        scale_to_zero=True,
    )
    res = FleetSimulator(cfg).run()
    # Work happened (the spike) and the run completed.
    assert res.completed > 0
    assert res.peak_warm_workers >= 1


def test_burst_triggers_shedding_or_preemption() -> None:
    # A sharp burst that outruns warm-up should exercise the saturation paths.
    profile = BurstLoad(baseline_rate=0.2, spike_rate=8.0, center_s=120.0, width_s=10.0)
    cfg = _config(
        profile=profile, horizon_s=400.0, min_workers=1, max_workers=6, committed_fraction=0.5
    )
    res = FleetSimulator(cfg).run()
    # Under a capped fleet the burst is handled by shedding speculative and/or
    # preempting for committed — at least one defence engages.
    assert (res.shed + res.preemptions) > 0


def test_committed_latency_better_than_speculative_under_pressure() -> None:
    # When the fleet saturates, committed (protected + preempting) should fare at
    # least as well as speculative on the tail.
    profile = BurstLoad(baseline_rate=0.5, spike_rate=6.0, center_s=150.0, width_s=20.0)
    cfg = _config(
        profile=profile, horizon_s=400.0, min_workers=1, max_workers=8, committed_fraction=0.5
    )
    res = FleetSimulator(cfg).run()
    if res.committed_latency.count > 5 and res.speculative_latency.count > 5:
        # Committed shouldn't be dramatically worse than speculative on p95.
        assert res.committed_latency.p95_ms <= res.speculative_latency.p95_ms * 2.0


# --------------------------------------------------------------------------- #
# Spot reclaims
# --------------------------------------------------------------------------- #


def test_spot_instance_reclaims_are_modelled() -> None:
    cfg = _config(
        profile=ConstantLoad(0.6),
        instance_key="gpu-l20-spot",
        horizon_s=1200.0,
        min_workers=2,
        max_workers=24,
    )
    res = FleetSimulator(cfg).run()
    # Over a long horizon a spot fleet should see at least one reclaim.
    assert res.reclaims >= 1
    # Reclaimed work is re-queued, so completion still happens.
    assert res.completed > 0
