"""Service + actuator wiring tests — observe -> plan -> actuate, no infra."""

from __future__ import annotations

from app.autoscale.actuator import KubernetesActuatorStub, RecordingActuator
from app.autoscale.clock import VirtualClock
from app.autoscale.lanes import Lane, QoSClass, default_lane_pools
from app.autoscale.service import AutoscaleService, build_autoscaler, build_service
from app.autoscale.signal import DemandSnapshot
from app.core.config import Settings


class _TraceProvider:
    """A demand provider that walks a fixed list of snapshots (then holds the last)."""

    def __init__(self, trace: list[DemandSnapshot]) -> None:
        self._trace = trace
        self._i = 0

    async def snapshot(self) -> DemandSnapshot:
        snap = self._trace[min(self._i, len(self._trace) - 1)]
        self._i += 1
        return snap


def _settings() -> Settings:
    return Settings(dashscope_api_key="test")


async def test_recording_actuator_applies_plan() -> None:
    actuator = RecordingActuator(initial={Lane.PROVIDER: 4, Lane.CPU: 2, Lane.GPU: 0})
    autoscaler = build_autoscaler(_settings(), clock=VirtualClock())
    snap = DemandSnapshot(
        depth_by_qos={QoSClass.COMMITTED: 40},
        inflight_by_lane={Lane.PROVIDER: 0},
        latency_samples_s={Lane.PROVIDER: [5.0]},
    )
    plan = autoscaler.plan(snap)
    applied = await actuator.apply(plan)
    assert applied.desired[Lane.PROVIDER] == plan.desired[Lane.PROVIDER]
    assert (await actuator.current_replicas())[Lane.PROVIDER] == plan.desired[Lane.PROVIDER]


async def test_actuator_is_idempotent() -> None:
    actuator = RecordingActuator(initial={Lane.PROVIDER: 8})
    autoscaler = build_autoscaler(_settings(), clock=VirtualClock())
    snap = DemandSnapshot(depth_by_qos={QoSClass.COMMITTED: 0})
    plan = autoscaler.plan(snap)
    await actuator.apply(plan)
    first = await actuator.current_replicas()
    await actuator.apply(plan)  # re-apply same plan
    assert await actuator.current_replicas() == first


async def test_service_tick_drives_full_loop() -> None:
    clock = VirtualClock()
    trace = [
        DemandSnapshot(
            depth_by_qos={QoSClass.COMMITTED: 40},
            inflight_by_lane={Lane.PROVIDER: 0},
            latency_samples_s={Lane.PROVIDER: [5.0]},
        ),
        DemandSnapshot(depth_by_qos={QoSClass.COMMITTED: 0}),
    ]
    provider = _TraceProvider(trace)
    service = build_service(provider, settings=_settings(), clock=clock)
    assert isinstance(service, AutoscaleService)

    plan1, _applied1 = await service.tick()
    assert service.ticks == 1
    # Scaled out on the heavy first snapshot.
    assert plan1.desired[Lane.PROVIDER] >= 4

    clock.advance(120.0)  # clear the cooldown
    plan2, _ = await service.tick()
    assert plan2.desired[Lane.PROVIDER] <= plan1.desired[Lane.PROVIDER]


def test_build_autoscaler_reads_additive_settings() -> None:
    s = _settings()
    autoscaler = build_autoscaler(s, clock=VirtualClock())
    pools = autoscaler.pools
    assert pools[Lane.PROVIDER].min_replicas == s.autoscale_provider_min
    assert pools[Lane.CPU].max_replicas == s.autoscale_cpu_max
    assert autoscaler.config.hysteresis_band == s.autoscale_hysteresis_band


def test_build_autoscaler_tolerates_missing_settings_keys() -> None:
    """A Settings-like object without the autoscale_* keys falls back to defaults."""

    class _Bare:
        pass

    autoscaler = build_autoscaler(_Bare(), clock=VirtualClock())  # type: ignore[arg-type]
    assert autoscaler.pools[Lane.PROVIDER].min_replicas == 4


def test_kubernetes_actuator_stub_is_unimplemented() -> None:
    stub = KubernetesActuatorStub(deployment_by_lane={Lane.PROVIDER: "render-provider"})
    assert stub.deployment_by_lane[Lane.PROVIDER] == "render-provider"


def test_default_pools_have_expected_lanes() -> None:
    pools = default_lane_pools()
    assert set(pools) == {Lane.CPU, Lane.PROVIDER, Lane.GPU}
    # GPU is the most expensive lane; CPU the cheapest.
    assert pools[Lane.GPU].cost_per_replica > pools[Lane.PROVIDER].cost_per_replica
    assert pools[Lane.PROVIDER].cost_per_replica > pools[Lane.CPU].cost_per_replica
