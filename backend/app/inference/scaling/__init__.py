"""Autoscaling + SLO routing brain for the inference gateway (facet C, kinora.md §12).

The elasticity + SLO brain that sits between the router (facet A) and the GPU
fleet. It decides **how much capacity to provision**, **where to route under
saturation**, and **what to shed** — and it proves those decisions against a
discrete-event simulation of a heterogeneous worker pool before they ever touch
real cloud capacity.

Public surface (imported lazily by callers to keep import cost low):

* :mod:`~app.inference.scaling.contracts` — the InferenceBackend / RouterMetrics
  Protocols facet C consumes from facet A, plus conforming test fakes.
* :mod:`~app.inference.scaling.instances` — heterogeneous GPU instance types +
  the cost model (cold-start, spot, per-second billing).
* :mod:`~app.inference.scaling.forecast` — demand forecasting (EWMA, double-EWMA
  trend, seasonal Holt-Winters, quantile headroom).
* :mod:`~app.inference.scaling.queueing` — queue-theory sizing (M/M/c reuse) +
  the latency-budget server count.
* :mod:`~app.inference.scaling.autoscaler` — the predictive autoscaler:
  scale-to-zero, warm-pool, anti-flap hysteresis, forecast lookahead.
* :mod:`~app.inference.scaling.pool` — the worker-pool state machine (cold→warm→
  busy→draining, spot reclaim).
* :mod:`~app.inference.scaling.simulator` — the discrete-event simulation harness.
* :mod:`~app.inference.scaling.routing` — SLO-driven backend selection.
* :mod:`~app.inference.scaling.shedding` — graceful load-shedding under saturation.
* :mod:`~app.inference.scaling.preemption` — priority preemption of speculative
  work for committed work (§4.4 zone priority).
* :mod:`~app.inference.scaling.pareto` — cost↔latency Pareto-frontier optimisation.
* :mod:`~app.inference.scaling.workload` — load profiles for the sim (constant,
  ramp, diurnal, burst, reader-population).
* :mod:`~app.inference.scaling.reports` — capacity-planning report assembly.
"""

from __future__ import annotations

# The package is intentionally lazy-import-friendly: callers import the concrete
# submodule they need (``from app.inference.scaling.autoscaler import ...``) to
# keep import cost low, mirroring ``app.reliability``. The names below are the
# curated public surface, re-exported for convenience; importing this package does
# pull the submodules, so hot paths should import the submodule directly.
from app.inference.scaling.autoscaler import (
    PredictiveAutoscaler,
    ScaleAction,
    ScaleDecision,
    ScalingPolicy,
)
from app.inference.scaling.contracts import (
    BackendDescriptor,
    BackendHealth,
    BackendKind,
    BackendTelemetry,
    InferenceBackend,
    RouterMetricsSource,
)
from app.inference.scaling.controller import (
    FleetAutoscaleController,
    FleetScalePlan,
)
from app.inference.scaling.forecast import (
    EwmaForecaster,
    Forecast,
    Forecaster,
    HoltForecaster,
    HoltWintersForecaster,
)
from app.inference.scaling.instances import (
    DEFAULT_CATALOG,
    BillingModel,
    CostBreakdown,
    InstanceType,
    default_catalog,
)
from app.inference.scaling.pareto import (
    FleetCandidate,
    ParetoFrontier,
    ParetoPoint,
    ParetoSweep,
    default_candidates,
)
from app.inference.scaling.pool import Worker, WorkerPool, WorkerState
from app.inference.scaling.preemption import (
    InflightJob,
    PreemptionPlanner,
    PreemptionPolicy,
)
from app.inference.scaling.queueing import FleetSizing, size_fleet
from app.inference.scaling.reports import CapacityPlanner, CapacityReport
from app.inference.scaling.routing import (
    RoutingCandidate,
    RoutingDecision,
    RoutingPolicy,
    SLORouter,
)
from app.inference.scaling.shedding import LoadShedder, SheddingPolicy
from app.inference.scaling.simulator import (
    FleetSimulator,
    SimulationConfig,
    SimulationResult,
)
from app.inference.scaling.workload import (
    ArrivalGenerator,
    BurstLoad,
    CompositeLoad,
    ConstantLoad,
    DiurnalLoad,
    LoadProfile,
    RampLoad,
    RequestPriority,
    reader_population_load,
)

__all__ = [
    # contracts
    "BackendDescriptor",
    "BackendHealth",
    "BackendKind",
    "BackendTelemetry",
    "InferenceBackend",
    "RouterMetricsSource",
    # instances + cost
    "BillingModel",
    "CostBreakdown",
    "InstanceType",
    "DEFAULT_CATALOG",
    "default_catalog",
    # forecasting
    "Forecast",
    "Forecaster",
    "EwmaForecaster",
    "HoltForecaster",
    "HoltWintersForecaster",
    # queueing
    "FleetSizing",
    "size_fleet",
    # autoscaler
    "PredictiveAutoscaler",
    "ScalingPolicy",
    "ScaleAction",
    "ScaleDecision",
    # multi-backend controller
    "FleetAutoscaleController",
    "FleetScalePlan",
    # pool
    "Worker",
    "WorkerPool",
    "WorkerState",
    # workload
    "RequestPriority",
    "LoadProfile",
    "ConstantLoad",
    "RampLoad",
    "DiurnalLoad",
    "BurstLoad",
    "CompositeLoad",
    "ArrivalGenerator",
    "reader_population_load",
    # routing
    "SLORouter",
    "RoutingPolicy",
    "RoutingCandidate",
    "RoutingDecision",
    # shedding
    "LoadShedder",
    "SheddingPolicy",
    # preemption
    "PreemptionPlanner",
    "PreemptionPolicy",
    "InflightJob",
    # simulator
    "FleetSimulator",
    "SimulationConfig",
    "SimulationResult",
    # pareto + reports
    "ParetoSweep",
    "ParetoFrontier",
    "ParetoPoint",
    "FleetCandidate",
    "default_candidates",
    "CapacityPlanner",
    "CapacityReport",
]
