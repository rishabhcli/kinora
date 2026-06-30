"""Render-worker autoscaling — demand signal, controller, actuator seam, simulator.

A production-grade worker autoscaler for the render fabric (kinora.md §4.6/§4.9/§12.2).
It maps multi-dimensional render demand (queue depth by QoS class, reader-velocity
buffer-underrun risk, in-flight provider jobs, p95 render latency) into a per-lane
:class:`~app.autoscale.controller.ScalingPlan` via target-tracking + predictive
pre-warm, with min/max bounds, fast scale-out / slow cooldown'd scale-in,
hysteresis (no flapping), per-lane pools (cpu Ken-Burns / provider-bound / gpu),
and a cost-aware cap. An :class:`~app.autoscale.actuator.Actuator` seam applies the
plan (k8s/ECS/process — interface only). A deterministic simulator replays demand
traces (steady / spike / diurnal / ingest-burst) and proves the controller beats a
fixed-size baseline on underrun rate, idle waste, and oscillation.

Additive + side-effect-free on import; scales workers only — never video, never the
budget gate, never a credit.
"""

from __future__ import annotations

from app.autoscale.actuator import (
    Actuator,
    AppliedScaling,
    KubernetesActuatorStub,
    RecordingActuator,
)
from app.autoscale.clock import Clock, MonotonicClock, VirtualClock
from app.autoscale.controller import (
    AutoscalerConfig,
    LaneDecision,
    RenderAutoscaler,
    ScalingPlan,
)
from app.autoscale.lanes import (
    Lane,
    LanePool,
    QoSClass,
    default_lane_pools,
    lane_for_qos,
)
from app.autoscale.service import (
    AutoscaleService,
    DemandProvider,
    build_autoscaler,
    build_service,
)
from app.autoscale.signal import (
    DEFAULT_VIDEO_SECONDS_PER_SHOT,
    DemandSnapshot,
    LanePressure,
    SessionDemand,
    percentile,
)
from app.autoscale.simulator import (
    RunMetrics,
    ScenarioComparison,
    compare_scenario,
    default_scenarios,
    diurnal_trace,
    ingest_burst_trace,
    run_static,
    run_trace,
    spike_trace,
    steady_trace,
)

__all__ = [
    "DEFAULT_VIDEO_SECONDS_PER_SHOT",
    "Actuator",
    "AppliedScaling",
    "AutoscaleService",
    "AutoscalerConfig",
    "Clock",
    "DemandProvider",
    "DemandSnapshot",
    "KubernetesActuatorStub",
    "Lane",
    "LaneDecision",
    "LanePool",
    "LanePressure",
    "MonotonicClock",
    "QoSClass",
    "RecordingActuator",
    "RenderAutoscaler",
    "RunMetrics",
    "ScalingPlan",
    "ScenarioComparison",
    "SessionDemand",
    "VirtualClock",
    "build_autoscaler",
    "build_service",
    "compare_scenario",
    "default_lane_pools",
    "default_scenarios",
    "diurnal_trace",
    "ingest_burst_trace",
    "lane_for_qos",
    "percentile",
    "run_static",
    "run_trace",
    "spike_trace",
    "steady_trace",
]
