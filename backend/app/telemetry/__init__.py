"""Kinora telemetry — the observability layer above ``app.observability``.

``app.observability`` owns the low-level Prometheus registry, the per-shot /
per-session emit helpers, and the env-gated OTel FastAPI instrumentation. This
package is the *meaning* layer built on top of it:

* **correlation / trace context** (:mod:`app.telemetry.context`) — contextvars +
  a structlog processor so every log line carries ``correlation_id`` /
  ``trace_id`` / ``span_id`` automatically;
* **a dependency-free tracer** (:mod:`app.telemetry.spans`) with W3C
  ``traceparent`` propagation and a transparent OpenTelemetry bridge — the
  default exporter is a no-op, so **nothing requires a collector**;
* **crew tracing** (:mod:`app.telemetry.crew`) — one span + one warehouse rollup
  per agent across showrunner→adapter→cinematographer→generator→critic→continuity;
* **RED / USE** helpers (:mod:`app.telemetry.red`, :mod:`app.telemetry.use`) for
  the API and the render workers;
* **the §13 warehouse** (:mod:`app.telemetry.warehouse`) — live per-agent
  quality/cost aggregation, mirrored to Prometheus gauges;
* **SLOs + burn-rate alerting** (:mod:`app.telemetry.slo`,
  :mod:`app.telemetry.alerts`) and **dashboards-as-code**
  (:mod:`app.telemetry.dashboards`).

Everything here is import-safe, offline-safe, and never calls a model.
"""

from __future__ import annotations

from app.telemetry.context import (
    bind_correlation_id,
    correlation_scope,
    current_context,
    get_correlation_id,
    get_span_id,
    get_trace_id,
    merge_correlation,
    new_correlation_id,
    reset_context,
)
from app.telemetry.crew import (
    CrewCall,
    agent_span,
    record_shot_outcome,
    traced_agent_call,
)
from app.telemetry.domain import (
    record_budget_burn,
    record_buffer_occupancy,
    record_conflict,
    record_conflict_resolved,
    record_qa,
    record_render_latency,
    record_render_mode,
    record_watermark_crossing,
)
from app.telemetry.exporters import (
    InMemorySpanExporter,
    LoggingSpanExporter,
    NullSpanExporter,
)
from app.telemetry.middleware import (
    CorrelationMiddleware,
    install_correlation_logging,
)
from app.telemetry.red import track_request
from app.telemetry.slo import SLO, SLOEvaluation, default_slos, slo_catalogue
from app.telemetry.spans import (
    Span,
    adopt_remote_context,
    extract_context,
    get_tracer,
    inject_context,
    span,
    start_span,
)
from app.telemetry.use import track_job
from app.telemetry.warehouse import (
    CREW_ROLES,
    AgentStats,
    MetricsWarehouse,
    get_warehouse,
    reset_warehouse,
)

__all__ = [
    "CREW_ROLES",
    "SLO",
    "AgentStats",
    "CorrelationMiddleware",
    "CrewCall",
    "InMemorySpanExporter",
    "LoggingSpanExporter",
    "MetricsWarehouse",
    "NullSpanExporter",
    "SLOEvaluation",
    "Span",
    "adopt_remote_context",
    "agent_span",
    "bind_correlation_id",
    "correlation_scope",
    "current_context",
    "default_slos",
    "extract_context",
    "get_correlation_id",
    "get_span_id",
    "get_trace_id",
    "get_tracer",
    "get_warehouse",
    "inject_context",
    "install_correlation_logging",
    "merge_correlation",
    "new_correlation_id",
    "record_budget_burn",
    "record_buffer_occupancy",
    "record_conflict",
    "record_conflict_resolved",
    "record_qa",
    "record_render_latency",
    "record_render_mode",
    "record_shot_outcome",
    "record_watermark_crossing",
    "reset_context",
    "reset_warehouse",
    "slo_catalogue",
    "span",
    "start_span",
    "track_job",
    "track_request",
    "traced_agent_call",
]
