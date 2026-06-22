"""Observability: Prometheus metrics (§12.5) and optional OpenTelemetry tracing.

Call sites import the small typed emit helpers (``observe_render_latency``,
``inc_cache``, ``set_buffer_occupancy``, ``observe_provider`` …) so hot paths
stay one-liners and never see Prometheus types. ``init_tracing`` is an env-gated
no-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
"""

from __future__ import annotations

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
from app.observability.tracing import init_tracing, tracing_enabled

__all__ = [
    "clear_session_metrics",
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
    "observe_provider",
    "observe_qa",
    "observe_render_latency",
    "record_request",
    "registry",
    "render_metrics",
    "set_app_info",
    "set_buffer_occupancy",
    "set_queue_depth",
    "tracing_enabled",
]
