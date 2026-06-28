"""Telemetry: the dependency-free tracer, exporters, and W3C propagation.

Runs offline with the OpenTelemetry SDK absent — the no-op path is exactly what
these assert. A fresh in-memory exporter is installed per test.
"""

from __future__ import annotations

import pytest

from app.telemetry import context as ctx
from app.telemetry import spans
from app.telemetry.exporters import (
    FanOutSpanExporter,
    InMemorySpanExporter,
    LoggingSpanExporter,
    NullSpanExporter,
)


@pytest.fixture(autouse=True)
def _fresh_tracer() -> InMemorySpanExporter:
    ctx.clear_context()
    exporter = InMemorySpanExporter()
    spans.set_tracer(spans.Tracer(exporter=exporter))
    return exporter


def _exporter() -> InMemorySpanExporter:
    ex = spans.get_tracer().exporter
    assert isinstance(ex, InMemorySpanExporter)
    return ex


def test_single_span_records_with_ok_status_and_duration() -> None:
    with spans.span("unit") as s:
        s.set_attribute("k", "v")
    finished = _exporter().finished_spans()
    assert len(finished) == 1
    assert finished[0].name == "unit"
    assert finished[0].status == spans.STATUS_OK
    assert finished[0].attributes["k"] == "v"
    assert finished[0].end_s is not None


def test_nested_spans_share_trace_and_link_parent() -> None:
    with spans.span("parent") as parent:
        with spans.span("child"):
            pass
        # After the child closes, the parent's span context is restored.
        assert ctx.get_span_id() == parent.span_id
    finished = {s.name: s for s in _exporter().finished_spans()}
    assert finished["child"].trace_id == finished["parent"].trace_id
    assert finished["child"].parent_id == finished["parent"].span_id
    assert finished["parent"].parent_id is None


def test_two_sibling_spans_nest_under_the_same_parent() -> None:
    with spans.span("root"):
        with spans.span("a"):
            pass
        with spans.span("b"):
            pass
    finished = {s.name: s for s in _exporter().finished_spans()}
    assert finished["a"].parent_id == finished["root"].span_id
    assert finished["b"].parent_id == finished["root"].span_id


def test_exception_marks_span_error_and_reraises() -> None:
    with pytest.raises(ValueError), spans.span("boom"):
        raise ValueError("kaboom")
    s = _exporter().finished_spans()[0]
    assert s.status == spans.STATUS_ERROR
    assert s.attributes["error.type"] == "ValueError"
    assert s.attributes["error.message"] == "kaboom"


def test_add_event_and_status_chaining() -> None:
    with spans.span("evented") as s:
        s.add_event("checkpoint", n=1).set_status(spans.STATUS_OK)
    rec = _exporter().finished_spans()[0]
    assert rec.events[0]["name"] == "checkpoint"
    assert rec.events[0]["attributes"]["n"] == 1


def test_attribute_values_are_coerced_to_primitives() -> None:
    with spans.span("coerce") as s:
        s.set_attribute("obj", {"a": 1})
    rec = _exporter().finished_spans()[0]
    assert isinstance(rec.attributes["obj"], str)


def test_to_dict_is_json_safe() -> None:
    with spans.span("d") as s:
        s.set_attribute("x", 1)
    rec = _exporter().finished_spans()[0]
    d = rec.to_dict()
    assert d["name"] == "d"
    assert d["status"] == "ok"
    assert "duration_ms" in d


# --------------------------------------------------------------------------- #
# W3C propagation
# --------------------------------------------------------------------------- #


def test_inject_and_parse_roundtrip() -> None:
    with spans.span("producer"):
        carrier = spans.inject_context()
    assert spans.TRACEPARENT_HEADER in carrier
    assert "x-correlation-id" in carrier
    trace_id, span_id = spans.parse_traceparent(carrier[spans.TRACEPARENT_HEADER])
    assert trace_id is not None and span_id is not None


def test_adopt_remote_context_continues_trace() -> None:
    with spans.span("producer"):
        carrier = spans.inject_context()
    parent_trace, _ = spans.parse_traceparent(carrier[spans.TRACEPARENT_HEADER])
    ctx.clear_context()

    tokens = spans.adopt_remote_context(carrier)
    try:
        assert ctx.get_trace_id() == parent_trace
        with spans.span("consumer") as consumer:
            assert consumer.trace_id == parent_trace
    finally:
        ctx.reset_context(tokens)


def test_extract_context_is_case_insensitive() -> None:
    carrier = {"TraceParent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01", "X-Correlation-Id": "c1"}
    extracted = spans.extract_context(carrier)
    assert extracted.valid
    assert extracted.trace_id == "a" * 32
    assert extracted.correlation_id == "c1"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "00-tooshort-tooshort-01",
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",  # zero trace id
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # zero span id
    ],
)
def test_parse_traceparent_rejects_malformed(value: str | None) -> None:
    assert spans.parse_traceparent(value) == (None, None)


def test_extract_empty_carrier_is_invalid() -> None:
    assert not spans.extract_context(None).valid
    assert not spans.extract_context({}).valid


# --------------------------------------------------------------------------- #
# Exporters
# --------------------------------------------------------------------------- #


def test_null_exporter_drops_everything() -> None:
    spans.set_tracer(spans.Tracer(exporter=NullSpanExporter()))
    with spans.span("x"):
        pass
    # Nothing to assert other than: no crash, no in-memory state.
    assert isinstance(spans.get_tracer().exporter, NullSpanExporter)


def test_in_memory_ring_is_bounded() -> None:
    exporter = InMemorySpanExporter(capacity=3)
    spans.set_tracer(spans.Tracer(exporter=exporter))
    for i in range(10):
        with spans.span(f"s{i}"):
            pass
    assert len(exporter) == 3
    names = [s.name for s in exporter.finished_spans()]
    assert names == ["s7", "s8", "s9"]


def test_spans_for_trace_filters_by_trace_id() -> None:
    exporter = _exporter()
    with spans.span("t1"):
        pass
    ctx.clear_context()
    with spans.span("t2"):
        pass
    all_spans = exporter.finished_spans()
    trace_ids = {s.trace_id for s in all_spans}
    assert len(trace_ids) == 2
    for tid in trace_ids:
        assert len(exporter.spans_for_trace(tid)) == 1


def test_fanout_exporter_delivers_to_all() -> None:
    a = InMemorySpanExporter()
    b = InMemorySpanExporter()
    spans.set_tracer(spans.Tracer(exporter=FanOutSpanExporter(a, b)))
    with spans.span("fan"):
        pass
    assert len(a) == 1 and len(b) == 1


def test_logging_exporter_never_raises() -> None:
    spans.set_tracer(spans.Tracer(exporter=LoggingSpanExporter()))
    with spans.span("logged"):
        pass  # Pure smoke: a logging failure must not surface.


def test_otel_bridge_is_noop_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # No OTLP endpoint configured → the bridge must stay None and never import OTel.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = spans.Tracer(exporter=InMemorySpanExporter())
    assert tracer._otel() is None
    spans.set_tracer(tracer)
    with spans.span("noop"):
        pass
    assert len(tracer.exporter) == 1  # type: ignore[arg-type]


class _FakeOtelSpan:
    """Minimal stand-in for an OTel span so the bridge can be exercised offline."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.events: list[str] = []
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: object = None) -> None:
        self.events.append(name)

    def record_exception(self, exc: BaseException) -> None:
        self.events.append(f"exc:{type(exc).__name__}")

    def end(self) -> None:
        self.ended = True


class _FakeOtelTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeOtelSpan] = []

    def start_span(self, name: str, attributes: object = None) -> _FakeOtelSpan:
        span = _FakeOtelSpan(name)
        if isinstance(attributes, dict):
            span.attributes.update(attributes)
        self.spans.append(span)
        return span


def test_otel_bridge_mirrors_spans_when_a_tracer_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inject a fake OTel tracer (no real SDK needed) and assert the pure-Python
    # span also drives the bridged span's lifecycle.
    fake = _FakeOtelTracer()
    tracer = spans.Tracer(exporter=InMemorySpanExporter())
    monkeypatch.setattr(tracer, "_load_otel_tracer", lambda: fake)
    spans.set_tracer(tracer)

    with spans.span("bridged") as s:
        s.set_attribute("k", "v")
        s.add_event("checkpoint")

    # The in-process span exported as usual.
    assert len(tracer.exporter) == 1  # type: ignore[arg-type]
    # And the bridge opened + closed a matching OTel span carrying the attribute.
    assert len(fake.spans) == 1
    bridged = fake.spans[0]
    assert bridged.name == "bridged"
    assert bridged.attributes["k"] == "v"
    assert "checkpoint" in bridged.events
    assert bridged.ended is True
