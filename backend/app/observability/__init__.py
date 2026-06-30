"""Observability plane — metrics, tracing facade, log enrichment, timelines (§12).

Call sites import the small typed emit helpers (``observe_render_latency``,
``inc_cache``, ``set_buffer_occupancy``, ``observe_provider`` …) so hot paths
stay one-liners and never see Prometheus types. ``init_tracing`` is an env-gated
no-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.

On top of the metrics + env-gated OTel instrumentation this package adds the full
render/provider observability plane:

* **tracing facade** (:mod:`app.observability.facade`) — ``@traced`` / ``span`` /
  ``render_span`` / ``provider_span``: one span per render step / provider call
  that auto-records the matching Prometheus series on close and binds the domain
  ids onto the log context, so a single shot render produces one trace
  (scheduler → pipeline → providers → persist). Built on the dependency-free
  :mod:`app.telemetry.spans` tracer whose OpenTelemetry bridge activates only when
  an OTLP endpoint is configured.
* **log enrichment** (:mod:`app.observability.enrichment`) — a structlog processor
  injecting the bound book/session/shot/provider/render-state ids.
* **metrics read side** (:mod:`app.observability.registry`) — a typed snapshot of
  the registry with derived SLIs (provider error-rate, cache hit-ratio).
* **render-trace timeline** (:mod:`app.observability.timeline`) — reconstructs a
  shot's lifecycle from emitted spans, no external backend required.
* **DI seam** (:mod:`app.observability.plane`) — :class:`ObservabilityPlane`, wired
  by the composition root; everything defaults to cheap no-ops in tests.
"""

from __future__ import annotations

from app.observability.enrichment import (
    bind_render_context,
    clear_render_context,
    current_render_context,
    merge_render_context,
    render_context_processors,
    render_log_context,
    reset_render_context,
)
from app.observability.exposition import build_metrics_router
from app.observability.facade import provider_span, render_span, span, traced
from app.observability.metrics import (
    clear_session_metrics,
    inc_cache,
    inc_cancellations,
    inc_conflict,
    inc_dlq,
    inc_idle_period,
    inc_job,
    inc_promotions,
    inc_provider_tokens,
    inc_render_mode,
    inc_render_retries,
    inc_seek_event,
    inc_shot_accepted,
    inc_shot_degraded,
    inc_video_seconds,
    inc_watermark_crossing,
    observe_provider,
    observe_qa,
    observe_render_latency,
    record_request,
    registry,
    render_metrics,
    set_app_info,
    set_buffer_occupancy,
    set_queue_depth,
)
from app.observability.plane import ObservabilityPlane, reset_tracer_to_default
from app.observability.registry import HistogramSnapshot, MetricsSnapshot, snapshot
from app.observability.timeline import (
    RenderTimeline,
    TimelineNode,
    build_timeline,
    timelines_by_shot,
)
from app.observability.tracing import init_tracing, tracing_enabled

__all__ = [
    "HistogramSnapshot",
    "MetricsSnapshot",
    "ObservabilityPlane",
    "RenderTimeline",
    "TimelineNode",
    "bind_render_context",
    "build_metrics_router",
    "build_timeline",
    "clear_render_context",
    "clear_session_metrics",
    "current_render_context",
    "inc_cache",
    "inc_cancellations",
    "inc_conflict",
    "inc_dlq",
    "inc_idle_period",
    "inc_job",
    "inc_promotions",
    "inc_provider_tokens",
    "inc_render_mode",
    "inc_render_retries",
    "inc_seek_event",
    "inc_shot_accepted",
    "inc_shot_degraded",
    "inc_video_seconds",
    "inc_watermark_crossing",
    "init_tracing",
    "merge_render_context",
    "observe_provider",
    "observe_qa",
    "observe_render_latency",
    "provider_span",
    "record_request",
    "registry",
    "render_context_processors",
    "render_log_context",
    "render_metrics",
    "render_span",
    "reset_render_context",
    "reset_tracer_to_default",
    "set_app_info",
    "set_buffer_occupancy",
    "set_queue_depth",
    "snapshot",
    "span",
    "timelines_by_shot",
    "traced",
    "tracing_enabled",
]
