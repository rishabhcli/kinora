"""Tests for the hallucination guard (citation validation + coverage)."""

from __future__ import annotations

from app.assistant.context import AssembledContext, ContextAssembler
from app.assistant.grounding import GroundingGuard, GuardConfig, parse_markers, split_sentences
from app.assistant.prompts import REFUSAL_SENTINEL
from app.assistant.types import RetrievedSpan, SourceKind


def _assemble(*texts: str) -> AssembledContext:
    spans = [
        RetrievedSpan(
            span_id=f"s{i}", kind=SourceKind.PAGE, text=t, score=1.0 - i * 0.1, locator=f"p.{i}"
        )
        for i, t in enumerate(texts, start=1)
    ]
    return ContextAssembler().assemble(spans)


def test_parse_markers_dedupes_in_order() -> None:
    assert parse_markers("First [2] then [1] then [2] again") == [2, 1]


def test_split_sentences() -> None:
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_grounded_answer_full_coverage() -> None:
    ctx = _assemble("Elsa has a platinum braid.", "The castle is on the mountain.")
    guard = GroundingGuard()
    answer = guard.verify("Elsa has a braid [1]. The castle is on a mountain [2].", ctx)
    assert answer.grounded
    assert answer.citation_coverage == 1.0
    assert {c.marker for c in answer.citations} == {1, 2}
    assert not answer.refused


def test_invalid_marker_is_dropped_and_lowers_coverage() -> None:
    ctx = _assemble("Elsa has a platinum braid.")
    guard = GroundingGuard()
    # [9] is invalid (only [1] exists); that sentence is unsupported.
    answer = guard.verify("Elsa has a braid [1]. She flies to the moon [9].", ctx)
    assert answer.citation_coverage < 1.0
    assert any("moon" in s for s in answer.unsupported_sentences)
    assert {c.marker for c in answer.citations} == {1}


def test_strict_mode_drops_unsupported_sentence() -> None:
    ctx = _assemble("Elsa has a platinum braid.")
    guard = GroundingGuard(GuardConfig(strict=True))
    answer = guard.verify("Elsa has a braid [1]. She flies to the moon [9].", ctx)
    assert "moon" not in answer.text
    assert "braid" in answer.text


def test_refusal_is_marked() -> None:
    ctx = _assemble("Elsa has a braid.")
    guard = GroundingGuard()
    answer = guard.verify(REFUSAL_SENTINEL, ctx)
    assert answer.refused
    assert not answer.grounded
    assert answer.citation_coverage == 0.0


def test_declared_markers_cover_single_sentence() -> None:
    ctx = _assemble("Elsa has a platinum braid.")
    guard = GroundingGuard()
    # No inline marker, but the JSON contract declared [1].
    answer = guard.verify("Elsa has a platinum braid.", ctx, declared_markers=[1])
    assert answer.citation_coverage == 1.0
    assert {c.marker for c in answer.citations} == {1}


def test_uncited_answer_not_grounded() -> None:
    ctx = _assemble("Elsa has a braid.")
    guard = GroundingGuard()
    answer = guard.verify("Elsa is a powerful queen with magic.", ctx)
    assert not answer.grounded
    assert answer.citation_coverage == 0.0


def test_question_and_hedge_sentences_not_counted_factual() -> None:
    ctx = _assemble("Elsa has a braid.")
    guard = GroundingGuard()
    # Trailing question shouldn't drag coverage down.
    answer = guard.verify("Elsa has a braid [1]. Want to know more?", ctx)
    assert answer.citation_coverage == 1.0
