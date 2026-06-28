"""Telemetry: correlation/trace/span context vars + the structlog processor.

These run with no infrastructure — the context layer is pure contextvars.
"""

from __future__ import annotations

import asyncio

from app.telemetry import context as ctx


def _clear() -> None:
    ctx.clear_context()


def test_new_ids_are_w3c_shaped_and_nonzero() -> None:
    trace = ctx.new_trace_id()
    span = ctx.new_span_id()
    corr = ctx.new_correlation_id()
    assert len(trace) == 32 and int(trace, 16) != 0
    assert len(span) == 16 and int(span, 16) != 0
    assert corr.startswith("corr_") and len(corr) == len("corr_") + 12


def test_ids_are_unique_across_calls() -> None:
    traces = {ctx.new_trace_id() for _ in range(200)}
    spans = {ctx.new_span_id() for _ in range(200)}
    assert len(traces) == 200
    assert len(spans) == 200


def test_bind_generates_correlation_id_when_absent() -> None:
    _clear()
    tokens = ctx.bind_correlation_id()
    try:
        assert ctx.get_correlation_id() is not None
        assert ctx.get_correlation_id().startswith("corr_")  # type: ignore[union-attr]
    finally:
        ctx.reset_context(tokens)
        assert ctx.get_correlation_id() is None


def test_bind_uses_supplied_ids() -> None:
    _clear()
    tokens = ctx.bind_correlation_id("corr_abc", trace_id="t" * 32, span_id="s" * 16)
    try:
        assert ctx.get_correlation_id() == "corr_abc"
        assert ctx.get_trace_id() == "t" * 32
        assert ctx.get_span_id() == "s" * 16
        assert ctx.current_context() == {
            "correlation_id": "corr_abc",
            "trace_id": "t" * 32,
            "span_id": "s" * 16,
        }
    finally:
        ctx.reset_context(tokens)


def test_correlation_scope_restores_prior_context() -> None:
    _clear()
    outer = ctx.bind_correlation_id("corr_outer")
    try:
        with ctx.correlation_scope("corr_inner") as cid:
            assert cid == "corr_inner"
            assert ctx.get_correlation_id() == "corr_inner"
        # The inner scope restored the outer correlation id.
        assert ctx.get_correlation_id() == "corr_outer"
    finally:
        ctx.reset_context(outer)


def test_merge_correlation_processor_injects_bound_ids() -> None:
    _clear()
    with ctx.correlation_scope("corr_log", trace_id="a" * 32, span_id="b" * 16):
        event = ctx.merge_correlation(None, "info", {"event": "hi"})
    assert event["correlation_id"] == "corr_log"
    assert event["trace_id"] == "a" * 32
    assert event["span_id"] == "b" * 16


def test_merge_correlation_does_not_overwrite_explicit_values() -> None:
    _clear()
    with ctx.correlation_scope("corr_ctx"):
        event = ctx.merge_correlation(
            None,
            "info",
            {"event": "hi", "correlation_id": "explicit"},
        )
    assert event["correlation_id"] == "explicit"


def test_merge_correlation_noop_outside_scope() -> None:
    _clear()
    event = ctx.merge_correlation(None, "info", {"event": "hi"})
    assert "correlation_id" not in event


def test_context_propagates_across_await_and_tasks() -> None:
    async def child() -> str | None:
        await asyncio.sleep(0)
        return ctx.get_correlation_id()

    async def main() -> tuple[str | None, str | None]:
        _clear()
        with ctx.correlation_scope("corr_async"):
            direct = await child()
            # A child task spawned inside the scope inherits the context copy.
            spawned = await asyncio.create_task(child())
        return direct, spawned

    direct, spawned = asyncio.run(main())
    assert direct == "corr_async"
    assert spawned == "corr_async"


def test_context_logging_processors_returns_the_merge_processor() -> None:
    procs = ctx.context_logging_processors()
    assert ctx.merge_correlation in procs
