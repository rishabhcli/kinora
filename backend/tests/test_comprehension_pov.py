"""Unit tests for POV + unreliable-narrator classification (no network)."""

from __future__ import annotations

from app.agents.comprehension.pov import classify_pov, pov_changed
from app.agents.contracts import NarrativePerson


def test_first_person() -> None:
    a = classify_pov("I walked into the room and saw my reflection in the mirror.")
    assert a.person is NarrativePerson.FIRST


def test_second_person() -> None:
    a = classify_pov("You open the door. You see the long corridor stretch before you.")
    assert a.person is NarrativePerson.SECOND


def test_third_limited_focal_character() -> None:
    text = "Elsa felt the cold rise. Elsa knew she could not stay. She thought of Anna."
    a = classify_pov(text, canon_names={"Elsa", "Anna"})
    assert a.person is NarrativePerson.THIRD_LIMITED
    assert a.focal_character == "Elsa"


def test_third_omniscient_when_no_single_focal() -> None:
    text = "The kingdom slept. Snow fell over the rooftops. They did not stir."
    a = classify_pov(text)
    assert a.person is NarrativePerson.THIRD_OMNISCIENT
    assert a.focal_character is None


def test_unreliable_narrator_flagged() -> None:
    text = (
        "I swear I only had one drink. Perhaps two. Honestly, I could have sworn "
        "the door was locked, or so I thought."
    )
    a = classify_pov(text)
    assert a.unreliable is True


def test_reliable_narration_not_flagged() -> None:
    a = classify_pov("She crossed the bridge and entered the silent town square.")
    assert a.unreliable is False


def test_quoted_first_person_does_not_flip_narrator() -> None:
    # The "I" lives inside dialogue; the narration frame is third person.
    text = '"I will go," she said, and she turned away from him.'
    a = classify_pov(text)
    assert a.person in (NarrativePerson.THIRD_LIMITED, NarrativePerson.THIRD_OMNISCIENT)


def test_focal_not_in_canon_is_dropped() -> None:
    text = "Gandalf knew the truth. Gandalf felt the weight of it."
    a = classify_pov(text, canon_names={"Frodo"})  # Gandalf not in canon
    assert a.focal_character is None
    assert a.person is NarrativePerson.THIRD_OMNISCIENT


def test_pov_changed_ignores_unknown() -> None:
    assert pov_changed(NarrativePerson.FIRST, NarrativePerson.THIRD_LIMITED) is True
    assert pov_changed(NarrativePerson.FIRST, NarrativePerson.UNKNOWN) is False
    assert pov_changed(NarrativePerson.FIRST, NarrativePerson.FIRST) is False
