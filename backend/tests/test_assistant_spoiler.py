"""Tests for the spoiler horizon (kinora.md §8.5)."""

from __future__ import annotations

from app.assistant.spoiler import SpoilerHorizon, redact_future_text
from app.assistant.types import ReadingPosition, RetrievedSpan, SourceKind


def _span(span_id: str, ordinal: int) -> RetrievedSpan:
    return RetrievedSpan(span_id=span_id, kind=SourceKind.PAGE, text="x", ordinal=ordinal)


def test_ceiling_uses_explicit_beat() -> None:
    h = SpoilerHorizon()
    pos = ReadingPosition(book_id="b", beat_index=30)
    assert h.ceiling_for(pos) == 30


def test_ceiling_margin_widens_window() -> None:
    h = SpoilerHorizon(margin=3)
    pos = ReadingPosition(book_id="b", beat_index=30)
    assert h.ceiling_for(pos) == 33


def test_unset_position_is_conservative_book_start() -> None:
    h = SpoilerHorizon()
    pos = ReadingPosition(book_id="b")
    assert h.ceiling_for(pos) == 0


def test_finished_book_opens_ceiling() -> None:
    h = SpoilerHorizon()
    pos = ReadingPosition(book_id="b", allow_full_book=True)
    assert h.ceiling_for(pos) > 1_000_000


def test_gate_drops_future_spans() -> None:
    h = SpoilerHorizon()
    spans = [_span("past", 5), _span("now", 10), _span("future", 11)]
    pos = ReadingPosition(book_id="b", beat_index=10)
    decision = h.gate(spans, pos)
    kept_ids = {s.span_id for s in decision.kept}
    assert kept_ids == {"past", "now"}
    assert decision.drop_count == 1
    assert decision.dropped[0].span_id == "future"


def test_filter_returns_only_visible() -> None:
    h = SpoilerHorizon()
    spans = [_span("a", 1), _span("b", 100)]
    pos = ReadingPosition(book_id="b", beat_index=1)
    visible = h.filter(spans, pos)
    assert [s.span_id for s in visible] == ["a"]


def test_boundary_beat_is_visible() -> None:
    h = SpoilerHorizon()
    # A span at exactly the reader's beat is where they ARE — visible.
    assert h.is_visible(_span("here", 30), ceiling=30)
    assert not h.is_visible(_span("next", 31), ceiling=30)


def test_redact_future_text_scrubs_forecast_sentences() -> None:
    text = "She climbed the mountain. Later she would die there. The wind howled."
    out = redact_future_text(text)
    assert "would die" not in out
    assert "climbed the mountain" in out
    assert "wind howled" in out


def test_redact_keeps_clean_text() -> None:
    text = "Elsa reached the summit and raised her hands."
    assert redact_future_text(text) == text
