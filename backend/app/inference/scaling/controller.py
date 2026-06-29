"""Multi-backend autoscale controller (kinora.md §12.2) — the ops entry point.

:class:`~app.inference.scaling.autoscaler.PredictiveAutoscaler` sizes *one* backend.
A real gateway serves a model across *several* heterogeneous backends at once (an
on-demand pool + a spot pool + a fast-tier pool), and the operator wants **one**
control tick that reads the router's live metrics for all of them and emits a
combined plan. This module is that orchestrator.

It is the seam where facet C consumes facet A: it takes a
:class:`~app.inference.scaling.contracts.RouterMetricsSource` (facet A's published
telemetry) + a per-backend forecaster, runs each backend's autoscaler against its
own observed demand, and returns a :class:`FleetScalePlan` — a desired warm-worker
count per backend plus the deltas the orchestrator (ECS desired-count / a
supervisor / the simulator) applies. It also derives the demand signal from the
telemetry itself (inflight + queue drained at warm capacity → an offered req/s
estimate) so the controller is self-contained: feed it metrics, get a plan.

Pure given the metrics + an injected clock; no I/O, no provisioning side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.inference.scaling.autoscaler import (
    PredictiveAutoscaler,
    ScaleDecision,
    ScalingPolicy,
)
from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendId,
    BackendTelemetry,
    RouterMetricsSource,
)
from app.inference.scaling.forecast import EwmaForecaster, Forecaster

logger = get_logger("app.inference.scaling.controller")

__all__ = [
    "BackendRegistration",
    "FleetScalePlan",
    "FleetAutoscaleController",
    "demand_estimate_from_telemetry",
]


def demand_estimate_from_telemetry(
    telemetry: BackendTelemetry, descriptor: BackendDescriptor
) -> float:
    """Estimate the offered req/s a backend is currently absorbing, from telemetry.

    A backend with ``inflight`` executing + ``queue_depth`` waiting, each taking
    ``service_time_s`` on a worker serving ``concurrency`` at once, is absorbing
    roughly ``(inflight + queue_depth) / service_time_s`` requests/second of
    offered load (the rate that sustains that backlog). This converts a static
    snapshot into the demand signal the forecaster consumes.
    """
    backlog = telemetry.inflight + telemetry.queue_depth
    if backlog <= 0:
        return 0.0
    return backlog / descriptor.service_time_s


@dataclass
class BackendRegistration:
    """One backend under the controller's management: its autoscaler + forecaster."""

    descriptor: BackendDescriptor
    autoscaler: PredictiveAutoscaler
    forecaster: Forecaster
    last_observed_at: float = field(default=float("-inf"))


@dataclass(frozen=True, slots=True)
class FleetScalePlan:
    """The combined desired-count plan across all managed backends (one tick)."""

    decisions: dict[BackendId, ScaleDecision]

    @property
    def total_desired_workers(self) -> int:
        """Sum of desired warm workers across the fleet."""
        return sum(d.desired for d in self.decisions.values())

    @property
    def total_delta(self) -> int:
        """Net change in warm workers across the fleet this tick."""
        return sum(d.delta for d in self.decisions.values())

    @property
    def changed(self) -> bool:
        return any(d.changed for d in self.decisions.values())

    def scaling_up(self) -> list[BackendId]:
        return [bid for bid, d in self.decisions.items() if d.delta > 0]

    def scaling_down(self) -> list[BackendId]:
        return [bid for bid, d in self.decisions.items() if d.delta < 0]

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the ops dashboard / logs."""
        return {
            "total_desired_workers": self.total_desired_workers,
            "total_delta": self.total_delta,
            "changed": self.changed,
            "decisions": {bid: d.to_dict() for bid, d in self.decisions.items()},
        }


@dataclass
class FleetAutoscaleController:
    """Manages several backends' autoscalers from one router-metrics source.

    Register backends with :meth:`register` (or :meth:`register_simple`), then call
    :meth:`tick` on each control interval with the live metrics source. The
    controller advances each backend's forecaster with the demand it observes,
    decides each backend's desired count, and returns the combined plan.

    Pass an explicit ``clock`` (monotonic seconds) so the per-backend autoscaler
    cooldowns + idle timers are deterministic in tests.
    """

    forecast_horizon: int = 1
    clock: Any = None
    backends: dict[BackendId, BackendRegistration] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.monotonic

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register(
        self,
        *,
        descriptor: BackendDescriptor,
        policy: ScalingPolicy,
        forecaster: Forecaster | None = None,
        initial_workers: int = 0,
    ) -> None:
        """Bring a backend under management with its own policy + forecaster."""
        autoscaler = PredictiveAutoscaler(
            descriptor,
            policy,
            current=initial_workers or policy.min_workers,
            clock=self.clock,
        )
        self.backends[descriptor.backend_id] = BackendRegistration(
            descriptor=descriptor,
            autoscaler=autoscaler,
            forecaster=forecaster or EwmaForecaster(alpha=0.4),
        )

    def register_simple(
        self, descriptor: BackendDescriptor, *, policy: ScalingPolicy | None = None
    ) -> None:
        """Register a backend with a default policy (convenience for the common case)."""
        self.register(descriptor=descriptor, policy=policy or ScalingPolicy())

    def deregister(self, backend_id: BackendId) -> None:
        """Stop managing a backend (e.g. the router dropped it)."""
        self.backends.pop(backend_id, None)

    # ------------------------------------------------------------------ #
    # The control tick
    # ------------------------------------------------------------------ #

    def tick(self, metrics: RouterMetricsSource) -> FleetScalePlan:
        """Read the router's metrics for every managed backend → combined plan.

        For each backend known to *both* the controller and the metrics source:
        derive its offered demand from the snapshot, advance its forecaster,
        reconcile the observed warm count, and decide. Backends the metrics source
        no longer reports are left untouched (the orchestrator handles removal).
        """
        now = self.clock()
        known = set(metrics.backend_ids())
        decisions: dict[BackendId, ScaleDecision] = {}
        for backend_id, reg in self.backends.items():
            if backend_id not in known:
                continue
            telemetry = metrics.telemetry(backend_id)
            demand = demand_estimate_from_telemetry(telemetry, reg.descriptor)
            reg.forecaster.observe(demand)
            forecast = reg.forecaster.forecast(self.forecast_horizon)
            decision = reg.autoscaler.decide(
                forecast=forecast, observed_warm=telemetry.warm_workers
            )
            reg.last_observed_at = now
            decisions[backend_id] = decision
        plan = FleetScalePlan(decisions=decisions)
        if plan.changed:
            logger.info(
                "controller.tick",
                total_desired=plan.total_desired_workers,
                total_delta=plan.total_delta,
                up=plan.scaling_up(),
                down=plan.scaling_down(),
            )
        return plan
