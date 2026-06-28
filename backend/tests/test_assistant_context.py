"""Tests for context assembly + citation numbering."""

from __future__ import annotations

from app.assistant.context import ContextAssembler
from app.assistant.read_model import chunk_passages
from app.assistant.types import RetrievedSpan, SourceKind


def _span(span_id: str, text: str, score: float, locator: str = "") -> RetrievedSpan:
    return RetrievedSpan(
        span_id=span_id, kind=SourceKind.PAGE, text=text, locator=locator, score=score
    )


def test_assemble_numbers_in_ranked_order() -> None:
    spans = [
        _span("a", "alpha passage", 0.9, "p.1"),
        _span("b", "beta passage", 0.5, "p.2"),
    ]
    ctx = ContextAssembler().assemble(spans)
    assert ctx.marker_to_span[1].span_id == "a"
    assert ctx.marker_to_span[2].span_id == "b"
    assert "[1] (p.1) alpha passage" in ctx.block
    assert "[2] (p.2) beta passage" in ctx.block


def test_empty_input_is_empty_context() -> None:
    ctx = ContextAssembler().assemble([])
    assert ctx.is_empty
    assert ctx.block == ""
    assert ctx.span_ids == []


def test_token_budget_drops_low_value_spans() -> None:
    big = "word " * 500
    spans = [
        _span("keep", "short high value", 0.95),
        _span("drop", big, 0.05),
    ]
    ctx = ContextAssembler(token_budget=40).assemble(spans)
    ids = ctx.span_ids
    assert "keep" in ids
    # The low-value 500-word span shouldn't fit a 40-token budget alongside keep.
    assert "drop" not in ids


def test_citation_for_valid_and_invalid_marker() -> None:
    ctx = ContextAssembler().assemble([_span("a", "alpha", 0.9, "p.1")])
    cite = ctx.citation_for(1, quote="alpha")
    assert cite is not None
    assert cite.span_id == "a"
    assert cite.marker == 1
    assert ctx.citation_for(9) is None


def test_chunk_passages_splits_on_paragraphs() -> None:
    text = "Para one is here.\n\nPara two follows.\n\nPara three ends."
    chunks = chunk_passages(text, words_per_chunk=4)
    assert len(chunks) >= 2
    # Each chunk carries an increasing word-start offset.
    starts = [c[0] for c in chunks]
    assert starts == sorted(starts)


def test_chunk_passages_empty() -> None:
    assert chunk_passages("") == []
    assert chunk_passages("   \n  ") == []
