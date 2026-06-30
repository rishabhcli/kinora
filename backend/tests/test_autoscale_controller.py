"""Deterministic controller tests (kinora.md §4.6/§4.9/§12.2).

All time is driven by a VirtualClock — no sleeps, no wall-clock, no infra. Proves:
target-tracking convergence, predictive pre-warm on a velocity spike, hysteresis +
cooldown prevent flapping, and min/max bounds + the cost cap are respected.
"""

from __future__ import annotations

import pytest

from app.autoscale.clock import VirtualClock
from app.autoscale.controller import AutoscalerConfig, RenderAutoscaler
from app.autoscale.lanes import Lane, LanePool, QoSClass, default_lane_pools
from app.autoscale.signal import DemandSnapshot, SessionDemand


def _provider_only_pools(*, min_r: int = 2, max_r: int = 20) -> dict[Lane, LanePool]:
    """A single elastic PROVIDER lane (no ramp step) so target-tracking is exact."""
    return {
        Lane.PROVIDER: LanePool(
            lane=Lane.PROVIDER,
            min_replicas=min_r,
            max_replicas=max_r,
            jobs_per_worker=2.0,
            cost_per_replica=1.0,
            warmup_s=0.0,
            scale_out_step=0,
        )
    }


def _depth_snapshot(committed: int, *, provider_quota: int | None = None) -> DemandSnapshot:
    return DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: committed},
        inflight_by_lane={Lane.PROVIDER: 0},
        latency_samples_s={Lane.PROVIDER: [5.0]},
        sessions=(),
        provider_quota=provider_quota,
    )


# --------------------------------------------------------------------------- #
# Target-tracking convergence
# --------------------------------------------------------------------------- #


def test_target_tracking_converges_to_backlog() -> None:
    clock = VirtualClock()
    pools = _provider_only_pools(min_r=2, max_r=20)
    autoscaler = RenderAutoscaler(
        pools=pools, config=AutoscalerConfig(hysteresis_band=0.0), clock=clock
    )
    # Backlog of 20 jobs at 2 jobs/worker -> 10 workers.
    plan = autoscaler.plan(_depth_snapshot(20))
    assert plan.desired[Lane.PROVIDER] == 10
    assert plan.decisions[Lane.PROVIDER].reason == "scale-out"

    # Backlog clears -> converge back to the minimum (after cooldown).
    clock.advance(120.0)
    plan = autoscaler.plan(_depth_snapshot(0))
    assert plan.desired[Lane.PROVIDER] == 2
    assert plan.decisions[Lane.PROVIDER].reason == "scale-in"


def test_scale_out_is_immediate_no_cooldown() -> None:
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(),
        config=AutoscalerConfig(hysteresis_band=0.0, scale_in_cooldown_s=300.0),
        clock=clock,
    )
    autoscaler.plan(_depth_snapshot(10))  # -> 5
    # No clock advance: scale-out again must still be allowed immediately.
    plan = autoscaler.plan(_depth_snapshot(30))
    assert plan.desired[Lane.PROVIDER] == 15
    assert plan.decisions[Lane.PROVIDER].reason == "scale-out"


# --------------------------------------------------------------------------- #
# Predictive pre-warm on a velocity spike
# --------------------------------------------------------------------------- #


def test_predictive_prewarm_on_velocity_spike_before_queue_fills() -> None:
    """A near-dry buffer at high velocity warms capacity even with an empty queue."""
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=20),
        config=AutoscalerConfig(hysteresis_band=0.0, predictive_gain=2.0),
        clock=clock,
    )
    # Zero queue depth, but four readers sprinting with ~empty buffers -> high risk.
    sessions = tuple(
        SessionDemand(velocity_wps=12.0, committed_seconds_ahead=2.0) for _ in range(4)
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 0},
        inflight_by_lane={Lane.PROVIDER: 0},
        latency_samples_s={Lane.PROVIDER: [5.0]},
        sessions=sessions,
    )
    plan = autoscaler.plan(snap)
    # Pre-warm lifts above the minimum despite an empty queue (predictive, not reactive).
    assert plan.desired[Lane.PROVIDER] > 2


def test_higher_predictive_gain_warms_more_aggressively() -> None:
    """The gain knob is an *extra* urgency boost on top of look-ahead backlog.

    Look-ahead demand is always part of the effective backlog (provisioning for film
    the reader will need); ``predictive_gain`` adds further headroom proportional to
    aggregate underrun risk, so a higher gain yields a strictly larger pool for the
    same near-dry sessions.
    """
    sessions = tuple(
        SessionDemand(velocity_wps=12.0, committed_seconds_ahead=2.0) for _ in range(4)
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 0},
        inflight_by_lane={Lane.PROVIDER: 0},
        latency_samples_s={Lane.PROVIDER: [5.0]},
        sessions=sessions,
    )

    def _desired(gain: float) -> int:
        autoscaler = RenderAutoscaler(
            pools=_provider_only_pools(min_r=2, max_r=40),
            config=AutoscalerConfig(hysteresis_band=0.0, predictive_gain=gain),
            clock=VirtualClock(),
        )
        return autoscaler.plan(snap).desired[Lane.PROVIDER]

    low = _desired(0.0)
    high = _desired(4.0)
    assert high > low >= 2


# --------------------------------------------------------------------------- #
# Hysteresis + cooldown prevent flapping
# --------------------------------------------------------------------------- #


def test_hysteresis_ignores_small_scale_out_wobble() -> None:
    """A sub-band upward jitter must not trigger growth (scale-out is band-gated)."""
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=40),
        config=AutoscalerConfig(hysteresis_band=0.2, scale_in_cooldown_s=0.0),
        clock=clock,
    )
    autoscaler.plan(_depth_snapshot(40))  # -> 20
    clock.advance(120.0)
    # A small upward wobble (target 22) is inside the 20%*20=4 scale-out margin -> hold.
    plan = autoscaler.plan(_depth_snapshot(44))
    assert plan.desired[Lane.PROVIDER] == 20
    assert plan.decisions[Lane.PROVIDER].reason == "hold:hysteresis"


def test_absolute_hysteresis_floor_absorbs_boundary_jitter() -> None:
    """Near the floor, a one-job wobble must not flap the pool out (then back)."""
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=4, max_r=40),
        config=AutoscalerConfig(
            hysteresis_band=0.15, hysteresis_floor=1.0, scale_in_cooldown_s=60.0
        ),
        clock=clock,
    )
    sizes: list[int] = []
    # Depth wobbles 8<->9 (target 4<->5) every 10s; the absolute floor of 1 absorbs
    # the +1 scale-out, so the pool holds at 4 instead of flapping.
    for i in range(10):
        depth = 8 if i % 2 == 0 else 9
        plan = autoscaler.plan(_depth_snapshot(depth))
        sizes.append(plan.desired[Lane.PROVIDER])
        clock.advance(10.0)
    assert set(sizes) == {4}, sizes


def test_scale_in_blocked_during_cooldown() -> None:
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=40),
        config=AutoscalerConfig(hysteresis_band=0.0, scale_in_cooldown_s=60.0),
        clock=clock,
    )
    autoscaler.plan(_depth_snapshot(40))  # -> 20 (scale-out sets last_scale)
    # Backlog vanishes immediately, but we are inside the cooldown window.
    clock.advance(30.0)
    plan = autoscaler.plan(_depth_snapshot(0))
    assert plan.desired[Lane.PROVIDER] == 20
    assert plan.decisions[Lane.PROVIDER].reason == "hold:cooldown"
    # Once the cooldown elapses, scale-in proceeds.
    clock.advance(40.0)  # total 70s > 60s
    plan = autoscaler.plan(_depth_snapshot(0))
    assert plan.desired[Lane.PROVIDER] == 2
    assert plan.decisions[Lane.PROVIDER].reason == "scale-in"


def test_oscillating_demand_does_not_flap() -> None:
    """Alternating high/low demand inside the cooldown must not thrash the pool."""
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=40),
        config=AutoscalerConfig(hysteresis_band=0.1, scale_in_cooldown_s=60.0),
        clock=clock,
    )
    sizes: list[int] = []
    # Tick every 10s (well inside the 60s scale-in cooldown), demand flips each tick.
    for i in range(12):
        depth = 40 if i % 2 == 0 else 4
        plan = autoscaler.plan(_depth_snapshot(depth))
        sizes.append(plan.desired[Lane.PROVIDER])
        clock.advance(10.0)
    # Count scale-in events: with a 60s cooldown and 10s ticks, scale-in can fire at
    # most once per 6 ticks, so the pool cannot follow the every-tick oscillation.
    scale_ins = sum(1 for a, b in zip(sizes, sizes[1:], strict=False) if b < a)
    assert scale_ins <= 2, sizes


def test_warmup_guard_blocks_teardown_of_just_warmed_replica() -> None:
    clock = VirtualClock()
    pools = {
        Lane.GPU: LanePool(
            lane=Lane.GPU,
            min_replicas=0,
            max_replicas=4,
            jobs_per_worker=1.0,
            cost_per_replica=20.0,
            warmup_s=90.0,
            scale_in_step=1,
            scale_out_step=1,
        )
    }
    autoscaler = RenderAutoscaler(
        pools=pools,
        config=AutoscalerConfig(hysteresis_band=0.0, scale_in_cooldown_s=10.0),
        clock=clock,
    )
    snap_hi = DemandSnapshot(
        depth_by_qos={QoSClass.SPECULATIVE: 4},
        inflight_by_lane={Lane.GPU: 4},
        latency_samples_s={Lane.GPU: [5.0]},
    )
    # Note: GPU only serves via inflight here; route demand through its inflight.
    snap_hi = DemandSnapshot(
        depth_by_qos={},
        inflight_by_lane={Lane.GPU: 4},
        latency_samples_s={Lane.GPU: [5.0]},
    )
    autoscaler.plan(snap_hi)  # warms one GPU (ramp step 1)
    # Cooldown elapses but warm-up (90s) has not -> teardown blocked.
    clock.advance(20.0)
    snap_lo = DemandSnapshot(inflight_by_lane={Lane.GPU: 0}, latency_samples_s={Lane.GPU: [1.0]})
    plan = autoscaler.plan(snap_lo)
    assert plan.decisions[Lane.GPU].reason == "hold:warmup"
    # After warm-up, scale-in is allowed.
    clock.advance(90.0)
    plan = autoscaler.plan(snap_lo)
    assert plan.decisions[Lane.GPU].reason in {"scale-in", "steady"}


# --------------------------------------------------------------------------- #
# Bounds + cost cap
# --------------------------------------------------------------------------- #


def test_max_bound_respected_under_extreme_demand() -> None:
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=8),
        config=AutoscalerConfig(hysteresis_band=0.0),
        clock=clock,
    )
    plan = autoscaler.plan(_depth_snapshot(10_000))
    assert plan.desired[Lane.PROVIDER] == 8  # clamped to max


def test_min_bound_respected_with_no_demand() -> None:
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=3, max_r=8),
        config=AutoscalerConfig(hysteresis_band=0.0),
        clock=clock,
    )
    plan = autoscaler.plan(_depth_snapshot(0))
    assert plan.desired[Lane.PROVIDER] == 3  # never below min


def test_cost_cap_trims_expensive_lane_first() -> None:
    clock = VirtualClock()
    pools = default_lane_pools(
        cpu_min=2, cpu_max=24, provider_min=4, provider_max=16, gpu_min=2, gpu_max=8
    )
    # Tight cap forces a trim; GPU (cost 20) must be sacrificed before PROVIDER (4)/CPU (1).
    autoscaler = RenderAutoscaler(
        pools=pools,
        config=AutoscalerConfig(hysteresis_band=0.0, max_cost=80.0),
        clock=clock,
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 60, QoSClass.SPECULATIVE: 40},
        inflight_by_lane={Lane.PROVIDER: 8, Lane.GPU: 8, Lane.CPU: 10},
        latency_samples_s={Lane.PROVIDER: [30.0], Lane.GPU: [30.0], Lane.CPU: [10.0]},
    )
    plan = autoscaler.plan(snap)
    assert plan.cost_capped
    assert plan.total_cost <= 80.0 + 1e-9
    # GPU trimmed to its floor first.
    assert plan.desired[Lane.GPU] == pools[Lane.GPU].min_replicas
    # Committed-serving PROVIDER lane keeps headroom (protected last).
    assert plan.desired[Lane.PROVIDER] >= pools[Lane.PROVIDER].min_replicas


def test_cost_cap_never_trims_below_minimums() -> None:
    clock = VirtualClock()
    pools = default_lane_pools(gpu_min=2, gpu_max=8)
    autoscaler = RenderAutoscaler(
        pools=pools,
        config=AutoscalerConfig(hysteresis_band=0.0, max_cost=1.0),  # absurdly tight
        clock=clock,
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 100},
        inflight_by_lane={Lane.PROVIDER: 16},
        latency_samples_s={Lane.PROVIDER: [40.0]},
    )
    plan = autoscaler.plan(snap)
    for lane, pool in pools.items():
        assert plan.desired[lane] >= pool.min_replicas


def test_provider_quota_dampens_scale_out() -> None:
    """At provider quota, the controller holds — more workers would just 429."""
    clock = VirtualClock()
    autoscaler = RenderAutoscaler(
        pools=_provider_only_pools(min_r=2, max_r=40),
        config=AutoscalerConfig(hysteresis_band=0.0),
        clock=clock,
    )
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 60},
        inflight_by_lane={Lane.PROVIDER: 8},
        latency_samples_s={Lane.PROVIDER: [5.0]},
        provider_quota=8,  # fully saturated
    )
    plan = autoscaler.plan(snap)
    assert plan.decisions[Lane.PROVIDER].reason == "hold:provider-quota"


def test_config_validation_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        AutoscalerConfig(hysteresis_band=1.0)
    with pytest.raises(ValueError):
        AutoscalerConfig(scale_in_cooldown_s=-1.0)
    with pytest.raises(ValueError):
        LanePool(lane=Lane.CPU, min_replicas=5, max_replicas=2)
