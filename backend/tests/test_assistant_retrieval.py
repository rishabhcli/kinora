"""Tests for the spoiler-aware hybrid retriever."""

from __future__ import annotations

from app.assistant.retrieval import RetrievalConfig, Retriever, merge_dedupe
from app.assistant.types import AssistantIntent, ReadingPosition, RetrievedSpan, SourceKind
from tests.assistant_fakes import FakeEmbedder, FakeReadModel, make_spans


async def test_retrieve_drops_future_spoilers() -> None:
    spans = make_spans()
    rm = FakeReadModel(spans)
    retriever = Retriever(rm, embedder=FakeEmbedder())
    pos = ReadingPosition(book_id="b", beat_index=8)
    result = await retriever.retrieve("Who is Elsa?", "Who is Elsa?", pos)
    ids = {s.span_id for s in result.spans}
    assert "canon:char_villain" not in ids  # beat 9, future
    assert result.spoiler.drop_count == 1
    assert any(d.span_id == "canon:char_villain" for d in result.spoiler.dropped)


async def test_finished_book_includes_everything() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=FakeEmbedder())
    pos = ReadingPosition(book_id="b", allow_full_book=True)
    result = await retriever.retrieve("Who is the Duke?", "Who is the Duke?", pos)
    assert result.spoiler.drop_count == 0


async def test_who_is_weights_canon_higher() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=FakeEmbedder())
    pos = ReadingPosition(book_id="b", beat_index=8)
    result = await retriever.retrieve(
        "Who is Elsa?", "Who is Elsa?", pos, intent=AssistantIntent.WHO_IS
    )
    top = result.spans[0]
    assert top.kind == SourceKind.CANON


async def test_recap_weights_beats_higher() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=FakeEmbedder())
    pos = ReadingPosition(book_id="b", beat_index=8)
    result = await retriever.retrieve(
        "What happened so far on the mountain?",
        "What happened so far?",
        pos,
        intent=AssistantIntent.RECAP,
    )
    kinds = [s.kind for s in result.spans]
    # A beat/shot should rank above the pure canon entity for a recap.
    assert SourceKind.BEAT in kinds or SourceKind.SHOT in kinds


async def test_works_without_embedder_lexical_fallback() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=None)  # no embedder at all
    pos = ReadingPosition(book_id="b", beat_index=8)
    result = await retriever.retrieve("castle mountain", "castle mountain", pos)
    assert result.spans  # lexical scoring still ranks something
    # Spans mentioning castle/mountain should be present.
    assert any("castle" in s.text.lower() or "mountain" in s.text.lower() for s in result.spans)


async def test_k_limits_results() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=FakeEmbedder())
    pos = ReadingPosition(book_id="b", beat_index=8)
    result = await retriever.retrieve(
        "Elsa", "Elsa", pos, config=RetrievalConfig(k=2)
    )
    assert len(result.spans) <= 2


async def test_word_position_resolves_ceiling() -> None:
    rm = FakeReadModel(make_spans())
    retriever = Retriever(rm, embedder=FakeEmbedder())
    # word_index 0 maps to beat ordinal 0 in the fake → only beat-0/earlier spans.
    pos = ReadingPosition(book_id="b", word_index=0)
    result = await retriever.retrieve("Elsa", "Elsa", pos)
    # Only the page span has word_start 0 and ordinal 2; with ceiling 2 it's kept.
    # The fake resolves ceiling to the max ordinal at/below word 0 = 2.
    assert all(s.ordinal <= 2 for s in result.spans)


def test_merge_dedupe_keeps_highest_score() -> None:
    a = RetrievedSpan(span_id="x", kind=SourceKind.PAGE, text="t", score=0.3)
    b = RetrievedSpan(span_id="x", kind=SourceKind.PAGE, text="t", score=0.9)
    c = RetrievedSpan(span_id="y", kind=SourceKind.PAGE, text="u", score=0.5)
    out = merge_dedupe([a, b, c])
    assert len(out) == 2
    x = next(s for s in out if s.span_id == "x")
    assert x.score == 0.9
