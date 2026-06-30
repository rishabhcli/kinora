"""Deep health + SLO / error-budget tracking (kinora.md §12).

The reliability plane that answers the product's core promise — *"the next
page's film is ready before the reader gets there"* — as measurable objectives:

* A dependency **health-check framework** (:mod:`app.slo.health`): injectable
  async probes (db / redis / object-store / providers / mcp) with per-probe
  timeouts, parallel evaluation, liveness-vs-readiness, and **criticality** so a
  degraded *non-critical* dependency reports ``degraded`` (still ready) rather
  than ``down``.
* An **SLI/SLO engine** (:mod:`app.slo.engine` over :mod:`app.slo.windows` +
  :mod:`app.slo.sli` + :mod:`app.slo.objectives`): product SLOs
  (buffer-underrun-free reads %, render p95, shot success-rate, API
  availability) computed from live rolling metric streams.
* **Error-budget accounting** with multi-window fast/slow burn-rate **alerts**
  (the SRE workbook rule).
* A **release-gate** signal (:mod:`app.slo.gate`) the experiment / flag / canary
  systems consult: is there budget to ship / raise exposure?

Additive: nothing here is imported by existing modules; the call-site helpers in
:mod:`app.slo.service` are opt-in one-liners and the API router is appended to
``ROUTERS``. Distinct from ``app.reliability.slo`` (which gates a finished
load-test report) — this tracks a *running* service continuously.
"""

from __future__ import annotations

from app.slo.engine import SLOEngine, SLOStatus, build_default_engine, engine_from_settings
from app.slo.gate import GateConfig, GateDecision, GateResult, decide_gate
from app.slo.health import (
    Criticality,
    HealthProbe,
    HealthRegistry,
    HealthReport,
    HealthStatus,
    ProbeOutcome,
    ProbeResult,
)
from app.slo.objectives import (
    AlertSeverity,
    BudgetState,
    BurnAlert,
    LatencyObjective,
    MultiWindowBurnPolicy,
    Objective,
    burn_rate,
    default_burn_policy,
)
from app.slo.service import (
    build_health_registry,
    get_health_registry,
    get_slo_engine,
    observe_intent_latency_ms,
    observe_render_latency_ms,
    record_api_request,
    record_read,
    record_shot,
    set_health_registry,
    set_slo_engine,
)
from app.slo.sli import DEFAULT_SLIS, SLIDefinition, SLIType, SLIValue
from app.slo.windows import CounterStream, RatioWindow, SampleStream, SampleWindow

__all__ = [
    "DEFAULT_SLIS",
    "AlertSeverity",
    "BudgetState",
    "BurnAlert",
    "CounterStream",
    "Criticality",
    "GateConfig",
    "GateDecision",
    "GateResult",
    "HealthProbe",
    "HealthRegistry",
    "HealthReport",
    "HealthStatus",
    "LatencyObjective",
    "MultiWindowBurnPolicy",
    "Objective",
    "ProbeOutcome",
    "ProbeResult",
    "RatioWindow",
    "SLIDefinition",
    "SLIType",
    "SLIValue",
    "SLOEngine",
    "SLOStatus",
    "SampleStream",
    "SampleWindow",
    "build_default_engine",
    "build_health_registry",
    "burn_rate",
    "decide_gate",
    "default_burn_policy",
    "engine_from_settings",
    "get_health_registry",
    "get_slo_engine",
    "observe_intent_latency_ms",
    "observe_render_latency_ms",
    "record_api_request",
    "record_read",
    "record_shot",
    "set_health_registry",
    "set_slo_engine",
]
