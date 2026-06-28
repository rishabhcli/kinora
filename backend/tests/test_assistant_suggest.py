"""Tests for grounded suggested-question generation."""

from __future__ import annotations

from app.assistant.suggest import suggest_questions
from app.assistant.types import AssistantIntent, RetrievedSpan
from tests.assistant_fakes import make_spans


def _visible_spans() -> list[RetrievedSpan]:
    # The 5 spans at ordinal <= 2 (drop the future Duke at ordinal 9).
    return [s for s in make_spans() if s.ordinal <= 2]


def test_suggestions_include_who_is_for_canon_entities() -> None:
    suggestions = suggest_questions(_visible_spans())
    who_is = [s for s in suggestions if s.intent is AssistantIntent.WHO_IS]
    assert who_is
    names = {s.text for s in who_is}
    assert any("Elsa" in n for n in names)


def test_suggestions_include_recap_when_narrative_present() -> None:
    suggestions = suggest_questions(_visible_spans())
    assert any(s.intent is AssistantIntent.RECAP for s in suggestions)


def test_suggestions_exclude_just_asked_question() -> None:
    suggestions = suggest_questions(_visible_spans(), asked="Who is Elsa?")
    assert all(s.text.lower() != "who is elsa?" for s in suggestions)


def test_suggestions_deduped_and_limited() -> None:
    suggestions = suggest_questions(_visible_spans(), limit=2)
    assert len(suggestions) <= 2
    texts = [s.text for s in suggestions]
    assert len(texts) == len(set(texts))


def test_suggestions_carry_entity_key() -> None:
    suggestions = suggest_questions(_visible_spans())
    elsa = next((s for s in suggestions if "Elsa" in s.text), None)
    assert elsa is not None
    assert elsa.about_entity_key == "char_elsa"


def test_no_spans_no_suggestions() -> None:
    assert suggest_questions([]) == []


def test_location_state_suggestion() -> None:
    suggestions = suggest_questions(_visible_spans())
    state = [s for s in suggestions if s.intent is AssistantIntent.STATE]
    # The fake has an Ice Castle location, so a state suggestion may appear.
    assert all(s.about_entity_key == "loc_castle" for s in state)
