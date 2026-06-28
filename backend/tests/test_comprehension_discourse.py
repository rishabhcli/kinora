"""Unit tests for discourse-mode + interiority detection (no network).

Covers dialogue-dominant, explicit interior monologue, free indirect discourse,
and plain narration, plus the subjective-staging helper.
"""

from __future__ import annotations

from app.agents.comprehension.discourse import classify_discourse, is_subjective
from app.agents.contracts import DiscourseMode


def test_dialogue_dominant() -> None:
    a = classify_discourse('"I will not leave without you," she said.')
    assert a.mode is DiscourseMode.DIALOGUE


def test_explicit_interior_monologue_thought_tag() -> None:
    a = classify_discourse("She wondered whether the door had ever truly been locked.")
    assert a.mode is DiscourseMode.INTERIOR_MONOLOGUE
    assert a.interiority is not None


def test_first_person_present_interior() -> None:
    a = classify_discourse("I cannot do this. My hands will not stop shaking.")
    assert a.mode is DiscourseMode.INTERIOR_MONOLOGUE


def test_free_indirect_discourse() -> None:
    # Third-person past with the character's own affect/idiom leaking through:
    # rhetorical questions + colloquial colouring, no "she thought" tag.
    text = (
        "She stared at the letter. How could he? After everything, surely he "
        "would not. No, it was absurd. Ridiculous, really."
    )
    a = classify_discourse(text)
    assert a.mode is DiscourseMode.FREE_INDIRECT
    assert a.fid_strength >= 0.5


def test_plain_narration() -> None:
    a = classify_discourse("The carriage rolled through the gates at dawn.")
    assert a.mode is DiscourseMode.NARRATION


def test_is_subjective_helper() -> None:
    assert is_subjective(DiscourseMode.FREE_INDIRECT) is True
    assert is_subjective(DiscourseMode.INTERIOR_MONOLOGUE) is True
    assert is_subjective(DiscourseMode.NARRATION) is False
    assert is_subjective(DiscourseMode.DIALOGUE) is False


def test_empty_text_is_narration() -> None:
    a = classify_discourse("")
    assert a.mode is DiscourseMode.NARRATION
