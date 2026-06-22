"""Optional OpenTelemetry tracing — fully env-gated, lazy, clean no-op.

Tracing is wired **only** when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. The
OpenTelemetry SDK / OTLP exporter / FastAPI instrumentation are imported *lazily*
inside :func:`init_tracing`, so the dependencies are optional: when the env var
is unset — or the packages are not installed — initialization is a clean no-op
that never raises and never touches the network. Install the extra to enable it:

    pip install 'kinora-backend[otel]'

The exporter, sampler, and resource attributes are read from the standard
``OTEL_*`` environment variables (e.g. ``OTEL_SERVICE_NAME``,
``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_HEADERS``).
"""

from __future__ import annotations

import os
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.observability.tracing")

#: The single env var that gates tracing on; unset → no-op.
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

#: Process-level guard so repeated ``init_tracing`` calls instrument once.
_initialized = False


def tracing_enabled() -> bool:
    """True when the OTLP endpoint env var is set (tracing is requested)."""
    return bool(os.environ.get(OTLP_ENDPOINT_ENV, "").strip())


def init_tracing(app: Any, *, service_name: str | None = None) -> bool:
    """Set up OTLP tracing + FastAPI instrumentation iff configured.

    Returns ``True`` when tracing was actually wired, ``False`` for the no-op
    path (env var unset, deps missing, or already initialized). Any failure to
    import/configure OpenTelemetry is swallowed and logged so a misconfigured or
    absent tracing stack can never break application startup.
    """
    global _initialized
    if not tracing_enabled():
        return False
    if _initialized:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning(
            "tracing.deps_missing",
            error=str(exc),
            hint="install kinora-backend[otel] to enable OTLP tracing",
        )
        return False

    try:
        resource_attrs: dict[str, str] = {}
        name = service_name or os.environ.get("OTEL_SERVICE_NAME")
        if name:
            resource_attrs["service.name"] = name
        provider = TracerProvider(resource=Resource.create(resource_attrs or None))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001 - tracing must never break startup
        logger.warning("tracing.init_failed", error=str(exc))
        return False

    _initialized = True
    logger.info("tracing.enabled", endpoint=os.environ.get(OTLP_ENDPOINT_ENV))
    return True


__all__ = ["OTLP_ENDPOINT_ENV", "init_tracing", "tracing_enabled"]
