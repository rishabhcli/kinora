"""Telemetry: crew tracing — per-agent spans + warehouse rollups."""

from __future__ import annotations

import asyncio

import pytest

from app.telemetry import context as ctx
from app.telemetry import crew, spans
from app.telemetry.exporters import InMemorySpanExporter
from app.telemetry.warehouse import MetricsWarehouse


@pytest.fixture(autouse=True)
def _fresh() -> InMemorySpanExporter:
    ctx.clear_context()
    exporter = InMemorySpanExporter()
    spans.set_tracer(spans.Tracer(exporter=exporter))
    return exporter


def _exporter() -> InMemorySpanExporter:
    ex = spans.get_tracer().exporter
    assert isinstance(ex, InMemorySpanExporter)
    return ex


def test_agent_span_opens_span_and_records_into_warehouse() -> None:
    wh = MetricsWarehouse()
    with crew.agent_span("generator", operation="design_shot", warehouse=wh) as call:
        call.record(input_tokens=12, output_tokens=34, cost_usd=0.05, repaired=True)
    stats = wh.agent("generator")
    assert stats is not None
    assert stats.calls == 1
    assert stats.input_tokens == 12
    assert stats.output_tokens == 34
    assert stats.repairs == 1

    span = _exporter().finished_spans()[0]
    assert span.name == "crew.generator.design_shot"
    assert span.attributes["agent"] == "generator"
    assert span.attributes["operation"] == "design_shot"
    assert span.attributes["tokens.input"] == 12
    assert span.attributes["json.repaired"] is True


def test_agent_span_records_error_on_exception() -> None:
    wh = MetricsWarehouse()
    with pytest.raises(RuntimeError), crew.agent_span("critic", warehouse=wh):
        raise RuntimeError("critic blew up")
    stats = wh.agent("critic")
    assert stats is not None
    assert stats.errors == 1
    span = _exporter().finished_spans()[0]
    assert span.status == spans.STATUS_ERROR


def test_add_tokens_accumulates_for_tool_loops() -> None:
    wh = MetricsWarehouse()
    with crew.agent_span("adapter", warehouse=wh) as call:
        call.add_tokens(output_tokens=10)
        call.add_tokens(output_tokens=5)
    stats = wh.agent("adapter")
    assert stats is not None
    assert stats.output_tokens == 15


def test_crew_pipeline_spans_share_one_trace() -> None:
    wh = MetricsWarehouse()
    pipeline = ("showrunner", "adapter", "cinematographer", "generator", "critic", "continuity")
    with spans.span("negotiation"):
        for agent in pipeline:
            with crew.agent_span(agent, warehouse=wh):
                pass
    finished = _exporter().finished_spans()
    trace_ids = {s.trace_id for s in finished}
    assert len(trace_ids) == 1  # the whole crew negotiation is one trace
    # Every agent span is a child of the negotiation root.
    root = next(s for s in finished if s.name == "negotiation")
    for s in finished:
        if s.name.startswith("crew."):
            assert s.parent_id == root.span_id
    # And the warehouse has all six roles.
    snap = wh.snapshot()
    assert {a["role"] for a in snap["agents"]} == set(pipeline)


def test_traced_agent_call_derives_token_delta() -> None:
    wh = MetricsWarehouse()
    counter = {"tokens": 100}

    async def fake_call() -> str:
        counter["tokens"] += 42
        return "ok"

    async def run() -> str:
        return await crew.traced_agent_call(
            "showrunner",
            fake_call,
            operation="plan_production",
            tokens_before=counter["tokens"],
            tokens_after=lambda: counter["tokens"],
            warehouse=wh,
        )

    result = asyncio.run(run())
    assert result == "ok"
    stats = wh.agent("showrunner")
    assert stats is not None
    assert stats.output_tokens == 42


def test_record_qa_and_shot_outcome_helpers() -> None:
    wh = MetricsWarehouse()
    crew.record_qa(agent="generator", ccs=0.9, style_drift=0.04, warehouse=wh)
    crew.record_shot_outcome(agent="generator", accepted=True, video_seconds=4.0, warehouse=wh)
    stats = wh.agent("generator")
    assert stats is not None
    assert abs((stats.mean_ccs or 0) - 0.9) < 1e-9
    assert stats.shots_accepted == 1
    assert abs(stats.video_seconds - 4.0) < 1e-9


def test_set_attribute_writes_to_span_and_extra() -> None:
    wh = MetricsWarehouse()
    with crew.agent_span("continuity", warehouse=wh) as call:
        call.set_attribute("conflict.kind", "lost_sword")
    assert call.extra["conflict.kind"] == "lost_sword"
    span = _exporter().finished_spans()[0]
    assert span.attributes["conflict.kind"] == "lost_sword"
