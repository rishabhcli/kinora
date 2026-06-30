"""The observability plane — one DI seam the composition root can wire (§12).

A single handle that bundles the render/provider observability stack so the
composition root configures it in one place and everything downstream
(``facade.span`` / ``provider_span`` / ``render_span``, the timeline reader, the
metrics snapshot) shares the same posture. **Everything defaults to a cheap
no-op**, so a test or a local run that never touches the plane pays nothing:

* tracing keeps its dependency-free :class:`~app.telemetry.spans.Tracer` (the
  OTel bridge only activates when an OTLP endpoint is set);
* the span exporter defaults to the no-op :class:`NullSpanExporter` — turn on the
  in-memory ring (``collect_spans=True``) to make :meth:`timeline` reconstruct a
  shot's lifecycle;
* the Prometheus exposition is mounted only when ``metrics_enabled``;
* log enrichment is opt-in via :meth:`install_log_enrichment`.

The plane never opens a socket or calls a model on construction. ``from_settings``
reads the (additive) ``observability_*`` settings so the composition root can do
``ObservabilityPlane.from_settings(settings)`` and forget about the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.observability.exposition import build_metrics_router
from app.observability.registry import MetricsSnapshot, snapshot
from app.observability.timeline import RenderTimeline, build_timeline, timelines_by_shot
from app.telemetry.exporters import InMemorySpanExporter, NullSpanExporter, SpanExporter
from app.telemetry.spans import SpanData, Tracer, get_tracer, set_tracer

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.core.config import Settings


@dataclass(slots=True)
class ObservabilityPlane:
    """Wired (or no-op) observability for the render/provider stack.

    Construct via :meth:`from_settings` (production) or directly (tests). Holding
    the span exporter lets :meth:`timeline` reconstruct a shot's lifecycle without
    any external tracing backend.
    """

    #: Mount the flag-gated Prometheus exposition router.
    metrics_enabled: bool = True
    #: Keep finished spans in a bounded in-memory ring (enables :meth:`timeline`).
    collect_spans: bool = False
    #: The active span exporter (set by :meth:`install_tracing`; ``None`` until then).
    span_exporter: SpanExporter | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> ObservabilityPlane:
        """Build a plane from the additive ``observability_*`` settings."""
        return cls(
            metrics_enabled=getattr(settings, "observability_metrics_enabled", True),
            collect_spans=getattr(settings, "observability_collect_spans", False),
        )

    @classmethod
    def noop(cls) -> ObservabilityPlane:
        """A fully inert plane (no exposition, no span collection) — the test default."""
        return cls(metrics_enabled=False, collect_spans=False)

    # -- wiring ------------------------------------------------------------- #

    def install_tracing(self, *, capacity: int = 4096) -> SpanExporter:
        """Set the process tracer's exporter per :attr:`collect_spans`.

        With ``collect_spans`` on, an :class:`InMemorySpanExporter` is installed so
        :meth:`timeline` can read spans back; otherwise the no-op
        :class:`NullSpanExporter`. Idempotent: the chosen exporter is stored on the
        plane and returned. The dependency-free tracer (and its lazy OTel bridge)
        is untouched — only the in-process sink changes.
        """
        exporter: SpanExporter = (
            InMemorySpanExporter(capacity=capacity) if self.collect_spans else NullSpanExporter()
        )
        get_tracer().set_exporter(exporter)
        self.span_exporter = exporter
        return exporter

    def install_log_enrichment(self) -> None:
        """Splice the domain-id structlog processor into the logging chain.

        Reconfigures structlog so every log line carries the bound
        ``book_id`` / ``session_id`` / ``shot_id`` / ``provider`` / ``render_state``
        in addition to the correlation/trace/span ids. Safe to call once at
        startup; importing here keeps :mod:`app.core.logging` free of a hard
        dependency on this module.
        """
        import structlog

        from app.observability.enrichment import merge_render_context

        # Insert just before the final renderer so domain ids ride every line.
        config = structlog.get_config()
        processors = list(config.get("processors", []))
        if merge_render_context not in processors:
            insert_at = max(0, len(processors) - 1)
            processors.insert(insert_at, merge_render_context)
            structlog.configure(processors=processors)

    def metrics_router(self, *, path: str = "/metrics") -> APIRouter | None:
        """Return the flag-gated Prometheus exposition router (``None`` when off)."""
        return build_metrics_router(enabled=self.metrics_enabled, path=path)

    # -- read side ---------------------------------------------------------- #

    def metrics_snapshot(self) -> MetricsSnapshot:
        """A typed snapshot of the Prometheus registry (derived SLIs included)."""
        return snapshot()

    def _spans(self) -> list[SpanData]:
        exporter = self.span_exporter
        if isinstance(exporter, InMemorySpanExporter):
            return exporter.finished_spans()
        return []

    def timeline(self, *, trace_id: str | None = None) -> RenderTimeline:
        """Reconstruct a render timeline from the collected spans.

        Requires ``collect_spans`` (an in-memory exporter installed by
        :meth:`install_tracing`); otherwise this returns an empty timeline because
        no spans were retained.
        """
        return build_timeline(self._spans(), trace_id=trace_id)

    def timelines_by_shot(self) -> dict[str, RenderTimeline]:
        """One reconstructed timeline per shot id seen in the collected spans."""
        return timelines_by_shot(self._spans())

    def clear_spans(self) -> None:
        """Drop every collected span (call between sessions / in tests)."""
        exporter = self.span_exporter
        if isinstance(exporter, InMemorySpanExporter):
            exporter.clear()


def reset_tracer_to_default() -> None:
    """Reset the process tracer to a fresh no-op-exporter tracer (test teardown)."""
    set_tracer(Tracer())


__all__ = ["ObservabilityPlane", "reset_tracer_to_default"]
