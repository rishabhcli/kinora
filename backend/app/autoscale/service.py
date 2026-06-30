"""Async control-loop wrapper: observe -> plan -> actuate (kinora.md §12.1, §12.2).

Glues the three pieces into one runnable loop. A :class:`DemandProvider` (anything
that yields a :class:`~app.autoscale.signal.DemandSnapshot` — a live queue/scheduler
adapter in production, a trace iterator in tests) feeds the
:class:`~app.autoscale.controller.RenderAutoscaler`; the resulting
:class:`~app.autoscale.controller.ScalingPlan` is handed to an
:class:`~app.autoscale.actuator.Actuator`. The loop owns nothing time-based itself
beyond pacing — all decision state lives in the controller — so a single
:meth:`tick` is fully testable without sleeping.

:func:`build_autoscaler` reads the env-backed :class:`~app.core.config.Settings`
into a controller (additive ``autoscale_*`` keys), so the composition root wires a
production controller with one call. The service deliberately has **no** start
side-effects on import and never touches the budget or live-video gate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.autoscale.actuator import Actuator, AppliedScaling, RecordingActuator
from app.autoscale.clock import Clock, MonotonicClock
from app.autoscale.controller import AutoscalerConfig, RenderAutoscaler, ScalingPlan
from app.autoscale.lanes import default_lane_pools
from app.autoscale.signal import DemandSnapshot
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger("app.autoscale.service")

__all__ = ["AutoscaleService", "DemandProvider", "build_autoscaler", "build_service"]


@runtime_checkable
class DemandProvider(Protocol):
    """Yields the current demand observation (live queue read or trace step)."""

    async def snapshot(self) -> DemandSnapshot:  # pragma: no cover - protocol
        """Return the latest :class:`DemandSnapshot` to feed the controller."""
        ...


class AutoscaleService:
    """One observe -> plan -> actuate cycle, wrapped for repeated ticking.

    :meth:`tick` performs exactly one cycle and returns the plan that was applied
    (so a caller / scheduler can log or react). The loop is intentionally *pull*
    based: the orchestrator decides cadence; the service does not spawn timers.
    """

    def __init__(
        self,
        *,
        autoscaler: RenderAutoscaler,
        provider: DemandProvider,
        actuator: Actuator,
    ) -> None:
        self._autoscaler = autoscaler
        self._provider = provider
        self._actuator = actuator
        self._ticks = 0

    @property
    def ticks(self) -> int:
        return self._ticks

    @property
    def autoscaler(self) -> RenderAutoscaler:
        return self._autoscaler

    async def tick(self) -> tuple[ScalingPlan, AppliedScaling]:
        """Observe demand, compute a plan, apply it. Returns (plan, applied)."""
        snapshot = await self._provider.snapshot()
        plan = self._autoscaler.plan(snapshot)
        applied = await self._actuator.apply(plan)
        self._ticks += 1
        return plan, applied


def build_autoscaler(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
) -> RenderAutoscaler:
    """Construct a :class:`RenderAutoscaler` from env-backed settings (additive keys).

    Reads ``autoscale_*`` settings when present, falling back to the package
    defaults so it works even on a Settings instance without the new keys.
    """
    settings = settings or get_settings()
    lane_pools = default_lane_pools(
        cpu_min=int(getattr(settings, "autoscale_cpu_min", 2)),
        cpu_max=int(getattr(settings, "autoscale_cpu_max", 24)),
        provider_min=int(getattr(settings, "autoscale_provider_min", 4)),
        provider_max=int(getattr(settings, "autoscale_provider_max", 16)),
        gpu_min=int(getattr(settings, "autoscale_gpu_min", 0)),
        gpu_max=int(getattr(settings, "autoscale_gpu_max", 4)),
    )
    config = AutoscalerConfig(
        scale_in_cooldown_s=float(getattr(settings, "autoscale_scale_in_cooldown_s", 60.0)),
        hysteresis_band=float(getattr(settings, "autoscale_hysteresis_band", 0.15)),
        hysteresis_floor=float(getattr(settings, "autoscale_hysteresis_floor", 1.0)),
        predictive_gain=float(getattr(settings, "autoscale_predictive_gain", 1.0)),
        max_cost=float(getattr(settings, "autoscale_max_cost", float("inf"))),
        latency_slo_s=float(getattr(settings, "autoscale_latency_slo_s", 25.0)),
        underrun_horizon_s=float(getattr(settings, "autoscale_underrun_horizon_s", 30.0)),
    )
    return RenderAutoscaler(
        pools=lane_pools, config=config, clock=clock or MonotonicClock()
    )


def build_service(
    provider: DemandProvider,
    *,
    settings: Settings | None = None,
    actuator: Actuator | None = None,
    clock: Clock | None = None,
) -> AutoscaleService:
    """Wire a ready-to-tick :class:`AutoscaleService` from a demand provider."""
    autoscaler = build_autoscaler(settings, clock=clock)
    if actuator is None:
        actuator = RecordingActuator(initial=autoscaler.current)
    return AutoscaleService(autoscaler=autoscaler, provider=provider, actuator=actuator)
