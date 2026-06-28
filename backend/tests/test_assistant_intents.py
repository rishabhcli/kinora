"""Tests for intent classification + entity extraction."""

from __future__ import annotations

import pytest

from app.assistant.intents import classify_intent
from app.assistant.types import AssistantIntent


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Who is Elsa?", AssistantIntent.WHO_IS),
        ("who's the Snow Queen", AssistantIntent.WHO_IS),
        ("Tell me about the ice castle", AssistantIntent.WHO_IS),
        ("Explain this passage", AssistantIntent.EXPLAIN),
        ("what does this mean", AssistantIntent.EXPLAIN),
        ("What has happened so far?", AssistantIntent.RECAP),
        ("Give me a recap", AssistantIntent.RECAP),
        ("catch me up", AssistantIntent.RECAP),
        ("Where is Elsa right now?", AssistantIntent.STATE),
        ("Why did the wind pick up?", AssistantIntent.GENERAL),
    ],
)
def test_intent_classification(question: str, expected: AssistantIntent) -> None:
    assert classify_intent(question).intent is expected


def test_recap_beats_who_is_priority() -> None:
    # "what happened so far" contains "what" but recap must win.
    assert classify_intent("What happened so far to Elsa?").intent is AssistantIntent.RECAP


def test_who_is_extracts_name() -> None:
    result = classify_intent("Who is Elsa the Snow Queen?")
    assert result.intent is AssistantIntent.WHO_IS
    assert result.entity_name is not None
    assert "Elsa" in result.entity_name or "Snow Queen" in result.entity_name


def test_who_is_multiword_name() -> None:
    result = classify_intent("Tell me about the Snow Queen")
    assert result.intent is AssistantIntent.WHO_IS
    assert result.entity_name == "Snow Queen"


def test_general_without_name_has_no_entity() -> None:
    result = classify_intent("why did it happen")
    assert result.intent is AssistantIntent.GENERAL
    assert result.entity_name is None


def test_what_is_without_name_is_general_not_who_is() -> None:
    # "what is" with no capitalized name should not be misread as a who-is.
    assert classify_intent("what is going on").intent is AssistantIntent.GENERAL
