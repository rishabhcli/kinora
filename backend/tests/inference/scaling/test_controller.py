"""Unit tests for the multi-backend autoscale controller (app...controller)."""

from __future__ import annotations

import pytest

from app.inference.scaling.autoscaler import ScalingPolicy
from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendHealth,
    BackendKind,
    BackendTelemetry,
    FakeMetrics,
)
from app.inference.scaling.controller import (
    FleetAutoscaleController,
    demand_estimate_from_telemetry,
)
from app.reliability.latency import LatencyDigest


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _summary():  # type: ignore[no-untyped-def]
    d = LatencyDigest()
    d.record_ms(1000.0)
    return d.summary()


def _descriptor(
    backend_id: str, *, concurrency: int = 2, service_s: float = 5.0
) -> BackendDescriptor:
    return BackendDescriptor(
        backend_id=backend_id,
        kind=BackendKind.VIDEO,
        instance_type="gpu-a10",
        concurrency=concurrency,
        service_time_s=service_s,
    )


def _telemetry(
    backend_id: str, *, warm: int, inflight: int, queue: int,
    health: BackendHealth = BackendHealth.HEALTHY,
) -> BackendTelemetry:
    return BackendTelemetry(
        backend_id=backend_id,
        warm_workers=warm,
        inflight=inflight,
        queue_depth=queue,
        latency=_summary(),
        health=health,
    )


# --------------------------------------------------------------------------- #
# Demand estimate
# --------------------------------------------------------------------------- #


def test_demand_estimate_from_backlog() -> None:
    desc = _descriptor("b", service_s=5.0)
    tel = _telemetry("b", warm=2, inflight=4, queue=6)  # backlog 10, /5s = 2 req/s
    assert demand_estimate_from_telemetry(tel, desc) == pytest.approx(2.0)


def test_demand_estimate_zero_when_idle() -> None:
    desc = _descriptor("b")
    tel = _telemetry("b", warm=2, inflight=0, queue=0)
    assert demand_estimate_from_telemetry(tel, desc) == 0.0


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_register_and_deregister() -> None:
    ctl = FleetAutoscaleController(clock=FakeClock())
    ctl.register_simple(_descriptor("b1"))
    assert "b1" in ctl.backends
    ctl.deregister("b1")
    assert "b1" not in ctl.backends


# --------------------------------------------------------------------------- #
# Tick → combined plan
# --------------------------------------------------------------------------- #


def test_tick_scales_each_backend_from_metrics() -> None:
    clock = FakeClock()
    ctl = FleetAutoscaleController(clock=clock)
    policy = ScalingPolicy(
        min_workers=1, warm_pool=1, max_workers=24, target_tail_s=45.0,
        scale_to_zero=False, max_step=16,
    )
    ctl.register(descriptor=_descriptor("busy"), policy=policy, initial_workers=1)
    ctl.register(descriptor=_descriptor("idle"), policy=policy, initial_workers=1)

    metrics = FakeMetrics()
    metrics.set(_telemetry("busy", warm=1, inflight=2, queue=8))  # heavy backlog
    metrics.set(_telemetry("idle", warm=1, inflight=0, queue=0))  # nothing

    plan = ctl.tick(metrics)
    assert set(plan.decisions) == {"busy", "idle"}
    # The busy backend scales up; the idle one holds its floor.
    assert plan.decisions["busy"].desired >= 1
    assert plan.decisions["idle"].desired == 1
    assert "busy" in plan.scaling_up() or plan.decisions["busy"].desired > 1


def test_tick_ignores_backends_not_in_metrics() -> None:
    clock = FakeClock()
    ctl = FleetAutoscaleController(clock=clock)
    ctl.register_simple(_descriptor("managed"))
    ctl.register_simple(_descriptor("gone"))
    metrics = FakeMetrics()
    metrics.set(_telemetry("managed", warm=1, inflight=0, queue=0))
    plan = ctl.tick(metrics)
    assert "managed" in plan.decisions
    assert "gone" not in plan.decisions  # not reported => untouched


def test_plan_aggregates_totals() -> None:
    clock = FakeClock()
    ctl = FleetAutoscaleController(clock=clock)
    policy = ScalingPolicy(
        min_workers=1, warm_pool=1, max_workers=24, target_tail_s=45.0,
        scale_to_zero=False, max_step=16,
    )
    ctl.register(descriptor=_descriptor("a"), policy=policy, initial_workers=1)
    ctl.register(descriptor=_descriptor("b"), policy=policy, initial_workers=1)
    metrics = FakeMetrics()
    metrics.set(_telemetry("a", warm=1, inflight=2, queue=6))
    metrics.set(_telemetry("b", warm=1, inflight=2, queue=6))
    plan = ctl.tick(metrics)
    assert plan.total_desired_workers == sum(d.desired for d in plan.decisions.values())
    assert plan.to_dict()["total_desired_workers"] == plan.total_desired_workers


def test_repeated_ticks_track_rising_demand() -> None:
    clock = FakeClock()
    ctl = FleetAutoscaleController(clock=clock)
    policy = ScalingPolicy(
        min_workers=1, warm_pool=1, max_workers=64, target_tail_s=45.0,
        scale_to_zero=False, max_step=4,
    )
    ctl.register(descriptor=_descriptor("b"), policy=policy, initial_workers=1)
    metrics = FakeMetrics()

    desired_over_time = []
    for q in (2, 6, 12, 20):  # rising backlog across ticks
        metrics.set(_telemetry("b", warm=ctl.backends["b"].autoscaler.current, inflight=2, queue=q))
        plan = ctl.tick(metrics)
        desired_over_time.append(plan.decisions["b"].desired)
        clock.advance(15.0)
    # The fleet grows monotonically as demand rises (step-limited but increasing).
    assert desired_over_time[-1] > desired_over_time[0]


def test_tick_with_no_backends_is_empty_plan() -> None:
    ctl = FleetAutoscaleController(clock=FakeClock())
    plan = ctl.tick(FakeMetrics())
    assert plan.decisions == {}
    assert plan.total_desired_workers == 0
    assert not plan.changed
