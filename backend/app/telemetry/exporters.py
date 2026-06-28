"""Span exporters — where finished spans go.

The tracer (:mod:`app.telemetry.spans`) records spans into the active exporter
when they end. The **default is a no-op**, so nothing in the test suite or a
local run requires a collector, a network endpoint, or the OpenTelemetry SDK.

Three exporters ship here:

* :class:`NullSpanExporter` — drops every span (the default).
* :class:`InMemorySpanExporter` — keeps a bounded ring of finished spans so tests
  and the demo metrics panel can assert on / display the trace tree without any
  external system.
* :class:`LoggingSpanExporter` — emits one structured log line per finished span
  (handy when an operator wants traces in the log pipeline but has no collector).

Real OTLP export is handled separately by the OTel bridge in
:mod:`app.telemetry.spans` (only when the SDK + endpoint are present); these
exporters are the dependency-free fallback that always works.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Protocol

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.telemetry.spans import SpanData

logger = get_logger("app.telemetry.exporters")

#: Default cap on the in-memory span ring so a long-running process can never
#: accumulate spans without bound.
DEFAULT_RING_CAPACITY = 4096


class SpanExporter(Protocol):
    """Sink for finished spans. Implementations must never raise to the caller."""

    def export(self, span: SpanData) -> None:
        """Record one finished span."""
        ...


class NullSpanExporter:
    """Drops every span — the zero-dependency, zero-cost default."""

    def export(self, span: SpanData) -> None:  # noqa: D102 - protocol impl
        return None


class InMemorySpanExporter:
    """Keeps a bounded ring of finished spans for tests / the demo panel.

    Thread-safe (the render workers export from threads). The ring evicts the
    oldest span when full so memory stays bounded under load.
    """

    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._spans: deque[SpanData] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()

    def export(self, span: SpanData) -> None:
        with self._lock:
            self._spans.append(span)

    def finished_spans(self) -> list[SpanData]:
        """Return a snapshot list of recorded spans (oldest first)."""
        with self._lock:
            return list(self._spans)

    def spans_for_trace(self, trace_id: str) -> list[SpanData]:
        """Return the recorded spans belonging to one trace (oldest first)."""
        with self._lock:
            return [s for s in self._spans if s.trace_id == trace_id]

    def clear(self) -> None:
        """Drop every recorded span (call between tests)."""
        with self._lock:
            self._spans.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)


class LoggingSpanExporter:
    """Emits one structured log line per finished span.

    Useful when traces should land in the log pipeline but no collector exists.
    Never raises: a logging failure is swallowed so it cannot break a hot path.
    """

    def __init__(self, *, level: str = "info") -> None:
        self._level = level

    def export(self, span: SpanData) -> None:
        try:
            emit = getattr(logger, self._level, logger.info)
            emit(
                "telemetry.span",
                name=span.name,
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_id=span.parent_id,
                duration_ms=round(span.duration_s * 1000, 3),
                status=span.status,
                **{f"attr.{k}": v for k, v in span.attributes.items()},
            )
        except Exception:  # noqa: BLE001 - exporting must never break a hot path
            return None


class FanOutSpanExporter:
    """Fan a span out to several exporters (e.g. in-memory + logging)."""

    def __init__(self, *exporters: SpanExporter) -> None:
        self._exporters = list(exporters)

    def export(self, span: SpanData) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(span)
            except Exception:  # noqa: BLE001 - one bad sink can't break the rest
                continue


__all__ = [
    "DEFAULT_RING_CAPACITY",
    "FanOutSpanExporter",
    "InMemorySpanExporter",
    "LoggingSpanExporter",
    "NullSpanExporter",
    "SpanExporter",
]
