"""A dependency-free tracer with a transparent OpenTelemetry bridge.

This is the heart of the telemetry package: a span/tracer that works with **zero
third-party dependencies** and transparently *also* drives a real OpenTelemetry
span when the SDK is installed and an OTLP endpoint is configured. The pure-Python
path is the source of truth for Kinora's own in-process trace tree (used by the
crew warehouse and the demo panel); the OTel bridge, when present, mirrors each
span into a real tracer so traces show up in Jaeger / Tempo / Alibaba TraceApp.

Why both? OpenTelemetry is an optional extra (``kinora-backend[otel]``). The unit
suite runs with it absent. So the tracer must:

* generate W3C-shaped ids (so the two worlds share an id space),
* record spans into the active :class:`~app.telemetry.exporters.SpanExporter`
  (default no-op — nothing requires a collector),
* and, *iff* a real OTel ``Tracer`` is available, open a matching real span so
  context propagates across services through the standard OTLP path.

Context propagation across processes uses the W3C ``traceparent`` header format,
so a render job carried over Redis or an HTTP hop keeps the same trace id.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.telemetry import context as ctx
from app.telemetry.exporters import NullSpanExporter, SpanExporter

logger = get_logger("app.telemetry.spans")

#: W3C trace-context header name (cross-process propagation).
TRACEPARENT_HEADER = "traceparent"
#: The only ``traceparent`` version this implementation emits / parses.
_TRACEPARENT_VERSION = "00"
#: ``trace-flags`` byte meaning "sampled".
_FLAG_SAMPLED = "01"

# Span status strings (kept as plain strings so no enum leaks to call sites).
STATUS_UNSET = "unset"
STATUS_OK = "ok"
STATUS_ERROR = "error"

#: Attribute-value types we accept (everything else is coerced to ``str``).
_PRIMITIVE = (str, bool, int, float)


@dataclass(slots=True)
class SpanData:
    """An immutable-ish record of one finished (or in-flight) span."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_s: float
    end_s: float | None = None
    status: str = STATUS_UNSET
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        """Elapsed wall-clock seconds (0 while still open)."""
        if self.end_s is None:
            return 0.0
        return max(0.0, self.end_s - self.start_s)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe view (for the demo panel / the trace read endpoint)."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": round(self.duration_s * 1000, 3),
            "status": self.status,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }


def _coerce_attr(value: Any) -> Any:
    return value if isinstance(value, _PRIMITIVE) else str(value)


class Span:
    """A live span. Use via :meth:`Tracer.start_span` / :func:`start_span`.

    Setting status/attributes/events mutates the in-flight record; on
    :meth:`end` the span is finalized, exported, and (if bridged) the matching
    real OTel span is closed.
    """

    def __init__(
        self,
        tracer: Tracer,
        data: SpanData,
        *,
        otel_span: Any | None = None,
        otel_ctx_token: Any | None = None,
    ) -> None:
        self._tracer = tracer
        self.data = data
        self._otel_span = otel_span
        self._otel_ctx_token = otel_ctx_token
        self._ended = False

    @property
    def trace_id(self) -> str:
        return self.data.trace_id

    @property
    def span_id(self) -> str:
        return self.data.span_id

    def set_attribute(self, key: str, value: Any) -> Span:
        """Attach a key/value attribute (chainable)."""
        coerced = _coerce_attr(value)
        self.data.attributes[key] = coerced
        if self._otel_span is not None:
            with contextlib.suppress(Exception):
                self._otel_span.set_attribute(key, coerced)
        return self

    def set_attributes(self, attributes: Mapping[str, Any]) -> Span:
        """Attach several attributes at once (chainable)."""
        for key, value in attributes.items():
            self.set_attribute(key, value)
        return self

    def add_event(self, name: str, **attributes: Any) -> Span:
        """Record a timestamped event within the span (chainable)."""
        event = {
            "name": name,
            "t": time.time(),
            "attributes": {k: _coerce_attr(v) for k, v in attributes.items()},
        }
        self.data.events.append(event)
        if self._otel_span is not None:
            with contextlib.suppress(Exception):
                self._otel_span.add_event(name, attributes=event["attributes"])
        return self

    def set_status(self, status: str) -> Span:
        """Set the span status (``ok`` / ``error`` / ``unset``)."""
        self.data.status = status
        return self

    def record_exception(self, exc: BaseException) -> Span:
        """Mark the span errored and record the exception type/message."""
        self.set_status(STATUS_ERROR)
        self.set_attribute("error.type", type(exc).__name__)
        self.set_attribute("error.message", str(exc))
        if self._otel_span is not None:
            with contextlib.suppress(Exception):
                self._otel_span.record_exception(exc)
        return self

    def end(self) -> None:
        """Finalize, export, and (if bridged) close the matching OTel span."""
        if self._ended:
            return
        self._ended = True
        self.data.end_s = time.monotonic()
        if self.data.status == STATUS_UNSET:
            self.data.status = STATUS_OK
        self._tracer._finish(self)
        if self._otel_span is not None:
            with contextlib.suppress(Exception):
                self._otel_span.end()
        if self._otel_ctx_token is not None:
            self._tracer._detach_otel(self._otel_ctx_token)


class Tracer:
    """Creates spans, threads context, exports finished spans.

    A process holds one tracer (see :func:`get_tracer`). The tracer keeps an
    exporter (default :class:`NullSpanExporter`) and, lazily and only when the
    OTel SDK + an OTLP endpoint are configured, a real OTel ``Tracer`` it bridges
    each span onto.
    """

    def __init__(self, *, exporter: SpanExporter | None = None) -> None:
        # Note: an empty InMemorySpanExporter is falsy (``__len__`` == 0), so this
        # MUST be an explicit ``is None`` check rather than ``exporter or …``.
        self._exporter: SpanExporter = exporter if exporter is not None else NullSpanExporter()
        self._lock = threading.Lock()
        self._otel_checked = False
        self._otel_tracer: Any | None = None

    # -- exporter management ------------------------------------------------- #

    def set_exporter(self, exporter: SpanExporter) -> None:
        """Swap the active span exporter (e.g. to an in-memory ring in tests)."""
        with self._lock:
            self._exporter = exporter

    @property
    def exporter(self) -> SpanExporter:
        return self._exporter

    # -- span creation ------------------------------------------------------- #

    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        parent: SpanData | None = None,
    ) -> Span:
        """Open a span as a child of the active span (or a given ``parent``).

        Sets the trace/span context vars so logs + descendant spans inherit them.
        The caller is responsible for :meth:`Span.end`; prefer :func:`span`.
        """
        parent_trace = parent.trace_id if parent is not None else ctx.get_trace_id()
        parent_span = parent.span_id if parent is not None else ctx.get_span_id()

        trace_id = parent_trace or ctx.new_trace_id()
        span_id = ctx.new_span_id()
        # Ensure a correlation id exists for log correlation even if none was bound.
        if ctx.get_correlation_id() is None:
            ctx.bind_correlation_id()

        data = SpanData(
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_span,
            start_s=time.monotonic(),
            attributes={k: _coerce_attr(v) for k, v in (attributes or {}).items()},
        )
        ctx.set_trace_context(trace_id, span_id)

        otel_span, otel_token = self._maybe_open_otel(name, data)
        return Span(self, data, otel_span=otel_span, otel_ctx_token=otel_token)

    def _finish(self, span: Span) -> None:
        try:
            self._exporter.export(span.data)
        except Exception:  # noqa: BLE001 - export must never break a traced call
            logger.debug("telemetry.export_failed", span=span.data.name)

    # -- OTel bridge (lazy, guarded) ---------------------------------------- #

    def _otel(self) -> Any | None:
        """Return a real OTel tracer iff the SDK + an OTLP endpoint are present."""
        if self._otel_checked:
            return self._otel_tracer
        with self._lock:
            if self._otel_checked:
                return self._otel_tracer
            self._otel_checked = True
            self._otel_tracer = self._load_otel_tracer()
            return self._otel_tracer

    @staticmethod
    def _load_otel_tracer() -> Any | None:
        # Only bridge when tracing was actually requested (an endpoint is set);
        # this mirrors observability.tracing.tracing_enabled without importing it.
        from app.telemetry.spans import _otlp_endpoint_set  # self-import for patchability

        if not _otlp_endpoint_set():
            return None
        try:
            from opentelemetry import trace as _ot

            return _ot.get_tracer("kinora.telemetry")
        except Exception as exc:  # noqa: BLE001 - missing SDK degrades to pure path
            logger.debug("telemetry.otel_unavailable", error=str(exc))
            return None

    def _maybe_open_otel(self, name: str, data: SpanData) -> tuple[Any | None, Any | None]:
        tracer = self._otel()
        if tracer is None:
            return None, None
        try:
            span = tracer.start_span(name, attributes=dict(data.attributes))
        except Exception as exc:  # noqa: BLE001 - never let the bridge break a span
            logger.debug("telemetry.otel_span_failed", error=str(exc))
            return None, None
        # Attaching the span to the OTel context (so cross-library propagation
        # works) is best-effort and separately guarded: a span still mirrors even
        # if the context module is unavailable in this environment.
        token: Any | None = None
        try:
            from opentelemetry import context as _otctx
            from opentelemetry import trace as _ot

            token = _otctx.attach(_ot.set_span_in_context(span))
        except Exception:  # noqa: BLE001 - context attach is optional
            token = None
        return span, token

    @staticmethod
    def _detach_otel(token: Any) -> None:
        with contextlib.suppress(Exception):
            from opentelemetry import context as _otctx

            _otctx.detach(token)


def _otlp_endpoint_set() -> bool:
    """True when an OTLP endpoint is configured (gates the OTel bridge).

    Reads the standard OTel env var directly so the telemetry package has no hard
    dependency on the observability module; both read the same variable.
    """
    import os

    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


# --------------------------------------------------------------------------- #
# Process-wide tracer + ergonomic helpers.
# --------------------------------------------------------------------------- #

_tracer_lock = threading.Lock()
_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """Return the process-wide tracer (created on first use)."""
    global _tracer
    if _tracer is None:
        with _tracer_lock:
            if _tracer is None:
                _tracer = Tracer()
    return _tracer


def set_tracer(tracer: Tracer) -> None:
    """Replace the process-wide tracer (mainly for tests)."""
    global _tracer
    with _tracer_lock:
        _tracer = tracer


def start_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Span:
    """Open a span on the process tracer (caller must :meth:`Span.end`)."""
    return get_tracer().start_span(name, attributes=attributes)


@contextlib.contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Context-manage a span: open, restore context + auto-end + record errors.

    The trace/span context vars are restored to the *parent's* values on exit, so
    a sibling span opened afterwards nests under the parent rather than the span
    that just closed. An exception inside the block marks the span errored and is
    re-raised.
    """
    # Snapshot the parent's trace/span so we can restore exactly on exit.
    parent_trace = ctx.get_trace_id()
    parent_span = ctx.get_span_id()
    s = start_span(name, attributes=attributes)
    try:
        yield s
    except BaseException as exc:
        s.record_exception(exc)
        raise
    finally:
        s.end()
        # Re-bind the parent context (start_span overwrote it with the child's).
        if parent_trace is not None:
            ctx.set_trace_context(parent_trace, parent_span or "")
            if parent_span is None:
                ctx.set_span_id(None)
        else:
            ctx.set_span_id(parent_span)


# --------------------------------------------------------------------------- #
# W3C trace-context propagation (cross-process).
# --------------------------------------------------------------------------- #


def inject_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Write the active trace context into a carrier dict as ``traceparent``.

    Returns the carrier (created if ``None``). Used to stamp a render job / an
    outbound HTTP request so the downstream worker continues the same trace.
    Includes the correlation id under ``x-correlation-id`` for log stitching.
    """
    out = carrier if carrier is not None else {}
    trace_id = ctx.get_trace_id()
    span_id = ctx.get_span_id()
    if trace_id and span_id:
        out[TRACEPARENT_HEADER] = f"{_TRACEPARENT_VERSION}-{trace_id}-{span_id}-{_FLAG_SAMPLED}"
    corr = ctx.get_correlation_id()
    if corr:
        out["x-correlation-id"] = corr
    return out


@dataclass(frozen=True, slots=True)
class ExtractedContext:
    """The parsed remote trace context (``None`` fields when absent/invalid)."""

    trace_id: str | None = None
    span_id: str | None = None
    correlation_id: str | None = None

    @property
    def valid(self) -> bool:
        return self.trace_id is not None and self.span_id is not None


def parse_traceparent(value: str | None) -> tuple[str | None, str | None]:
    """Parse a W3C ``traceparent`` value into ``(trace_id, span_id)``.

    Returns ``(None, None)`` for anything malformed or for the invalid all-zero
    ids, so a forged or truncated header degrades to starting a fresh trace.
    """
    if not value:
        return None, None
    parts = value.strip().split("-")
    if len(parts) < 4:
        return None, None
    _version, trace_id, span_id, _flags = parts[0], parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(span_id) != 16:
        return None, None
    try:
        if int(trace_id, 16) == 0 or int(span_id, 16) == 0:
            return None, None
    except ValueError:
        return None, None
    return trace_id.lower(), span_id.lower()


def extract_context(carrier: Mapping[str, str] | None) -> ExtractedContext:
    """Parse a carrier's ``traceparent`` + correlation id (case-insensitive)."""
    if not carrier:
        return ExtractedContext()
    lower = {str(k).lower(): v for k, v in carrier.items()}
    trace_id, span_id = parse_traceparent(lower.get(TRACEPARENT_HEADER))
    corr = lower.get("x-correlation-id") or lower.get("correlation-id")
    return ExtractedContext(trace_id=trace_id, span_id=span_id, correlation_id=corr)


def adopt_remote_context(carrier: Mapping[str, str] | None) -> ctx.ContextTokens:
    """Bind a remote trace context onto this process (continue the trace).

    Call at a worker entrypoint after pulling a job: the worker's spans become
    children of the producer's span and logs share the producer's correlation id.
    Returns the reset tokens; pass to :func:`app.telemetry.context.reset_context`.
    """
    extracted = extract_context(carrier)
    return ctx.bind_correlation_id(
        extracted.correlation_id,
        trace_id=extracted.trace_id,
        span_id=extracted.span_id,
    )


__all__ = [
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_UNSET",
    "TRACEPARENT_HEADER",
    "ExtractedContext",
    "Span",
    "SpanData",
    "Tracer",
    "adopt_remote_context",
    "extract_context",
    "get_tracer",
    "inject_context",
    "parse_traceparent",
    "set_tracer",
    "span",
    "start_span",
]
