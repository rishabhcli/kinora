"""Observability facade — ``@traced`` / ``span`` / ``render_span`` / ``provider_span``.

A single shot render must produce *one* trace whose spans nest correctly, with
the matching Prometheus series auto-recorded on span close and the domain ids
bound onto the log context for the duration. Everything runs offline with the
OpenTelemetry SDK absent (the dependency-free tracer + in-memory exporter is the
source of truth here).
"""

from __future__ import annotations

import pytest

from app.observability import facade
from app.observability.enrichment import current_render_context
from app.observability.registry import snapshot
from app.telemetry import context as ctx
from app.telemetry import spans
from app.telemetry.exporters import InMemorySpanExporter


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


def test_one_shot_render_is_a_single_trace_with_nested_spans(
    _fresh_tracer: InMemorySpanExporter,
) -> None:
    with facade.render_span("render.shot", shot_id="shot-1", mode="i2v", book_id="bk"):
        with facade.provider_span("i2v", model="wan2.1-i2v-turbo", shot_id="shot-1"):
            pass
        with facade.span("persist", shot_id="shot-1"):
            pass

    finished = _fresh_tracer.finished_spans()
    assert {s.name for s in finished} == {"render.shot", "provider.i2v", "persist"}
    # One trace: every span shares the root's trace id.
    trace_ids = {s.trace_id for s in finished}
    assert len(trace_ids) == 1
    root = next(s for s in finished if s.name == "render.shot")
    children = [s for s in finished if s.name != "render.shot"]
    # The provider + persist spans are children of the render span.
    assert all(c.parent_id == root.span_id for c in children)
    assert root.parent_id is None


def test_domain_ids_are_set_as_span_attributes() -> None:
    with facade.render_span(
        "render.shot", shot_id="shot-9", mode="t2v", book_id="bk-2", session_id="se-3"
    ):
        pass
    span_data = _exporter().finished_spans()[0]
    assert span_data.attributes[facade.ATTR_SHOT] == "shot-9"
    assert span_data.attributes[facade.ATTR_BOOK] == "bk-2"
    assert span_data.attributes[facade.ATTR_SESSION] == "se-3"
    assert span_data.attributes[facade.ATTR_RENDER_MODE] == "t2v"


def test_domain_ids_are_bound_to_log_context_inside_and_cleared_after() -> None:
    assert current_render_context() == {}
    with facade.span("x", book_id="b", shot_id="s", provider="p"):
        ctx_inside = current_render_context()
        assert ctx_inside["book_id"] == "b"
        assert ctx_inside["shot_id"] == "s"
        assert ctx_inside["provider"] == "p"
    # Restored on exit — no leak into a sibling unit of work.
    assert current_render_context() == {}


def test_render_span_records_latency_on_success() -> None:
    # Unique mode label so the delta is isolated from the shared registry.
    mode = "facade_mode_ok"
    before = snapshot().histogram("kinora_render_latency_seconds", mode=mode).count
    with facade.render_span("render.shot", shot_id="s", mode=mode):
        pass
    after = snapshot().histogram("kinora_render_latency_seconds", mode=mode).count
    assert after == before + 1.0


def test_render_span_skips_latency_on_error_but_marks_span_errored() -> None:
    mode = "facade_mode_err"
    before = snapshot().histogram("kinora_render_latency_seconds", mode=mode).count
    with pytest.raises(RuntimeError), facade.render_span("render.shot", shot_id="s", mode=mode):
        raise RuntimeError("boom")
    after = snapshot().histogram("kinora_render_latency_seconds", mode=mode).count
    # A failed render's wall-clock must not pollute the latency SLI.
    assert after == before
    span_data = _exporter().finished_spans()[0]
    assert span_data.status == spans.STATUS_ERROR
    assert span_data.attributes["error.type"] == "RuntimeError"


def test_provider_span_records_call_and_latency_on_success() -> None:
    model, op = "facade-tts-model", "facade_tts"
    before = snapshot()
    with facade.provider_span(op, model=model, shot_id="s"):
        pass
    after = snapshot()
    assert (
        after.counter("kinora_provider_calls_total", model=model, op=op)
        == before.counter("kinora_provider_calls_total", model=model, op=op) + 1.0
    )
    assert (
        after.histogram("kinora_provider_latency_seconds", op=op).count
        == before.histogram("kinora_provider_latency_seconds", op=op).count + 1.0
    )
    assert after.counter("kinora_provider_errors_total", model=model, op=op) == 0.0


def test_provider_span_records_error_on_failure() -> None:
    model, op = "facade-i2v-model", "facade_i2v"
    before = snapshot()
    with pytest.raises(ValueError), facade.provider_span(op, model=model, shot_id="s"):
        raise ValueError("provider failed")
    after = snapshot()
    assert (
        after.counter("kinora_provider_calls_total", model=model, op=op)
        == before.counter("kinora_provider_calls_total", model=model, op=op) + 1.0
    )
    assert (
        after.counter("kinora_provider_errors_total", model=model, op=op)
        == before.counter("kinora_provider_errors_total", model=model, op=op) + 1.0
    )
    assert after.provider_error_rate(model=model, op=op) == 1.0


def test_traced_decorator_on_sync_function_opens_a_span() -> None:
    @facade.traced("unit.work")
    def work(x: int) -> int:
        return x * 2

    assert work(3) == 6
    finished = _exporter().finished_spans()
    assert [s.name for s in finished] == ["unit.work"]
    assert finished[0].status == spans.STATUS_OK


def test_traced_decorator_default_name_uses_qualname() -> None:
    @facade.traced()
    def labelled() -> None:
        return None

    labelled()
    assert _exporter().finished_spans()[0].name.endswith("labelled")


@pytest.mark.asyncio
async def test_traced_decorator_on_async_function_opens_a_span() -> None:
    @facade.traced("async.work")
    async def work(x: int) -> int:
        return x + 1

    assert await work(41) == 42
    finished = _exporter().finished_spans()
    assert [s.name for s in finished] == ["async.work"]


def test_traced_decorator_records_exception_and_reraises() -> None:
    @facade.traced("failing")
    def boom() -> None:
        raise KeyError("nope")

    with pytest.raises(KeyError):
        boom()
    assert _exporter().finished_spans()[0].status == spans.STATUS_ERROR


def test_sibling_spans_nest_under_the_same_parent() -> None:
    with facade.span("root", shot_id="s"):
        with facade.span("a"):
            pass
        with facade.span("b"):
            pass
    finished = {s.name: s for s in _exporter().finished_spans()}
    root = finished["root"]
    assert finished["a"].parent_id == root.span_id
    assert finished["b"].parent_id == root.span_id
