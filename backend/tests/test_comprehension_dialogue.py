"""Unit tests for dialogue attribution + speaker diarization (no network).

Tag-based attribution ("said X" / "X said"), nearest-name fallback, two-party
alternation, the §10 no-invent guardrail (canon filtering), and dialogue density.
"""

from __future__ import annotations

from app.agents.comprehension.dialogue import (
    attribute_dialogue,
    dialogue_density,
    to_dialogue_lines,
)


def test_tag_after_quote() -> None:
    attrs = attribute_dialogue('"We must leave now," said Elsa.')
    assert len(attrs) == 1
    assert attrs[0].speaker == "Elsa"
    assert attrs[0].method == "tag_after"
    assert attrs[0].inferred is False


def test_tag_before_quote() -> None:
    attrs = attribute_dialogue('Anna whispered, "Do not be afraid."')
    assert attrs[0].speaker == "Anna"
    assert attrs[0].method == "tag_before"


def test_two_party_alternation_fills_untagged_lines() -> None:
    text = (
        '"Where are you going?" asked Elsa. '
        '"To the mountain," said Anna. '
        '"Alone?" '
        '"Yes, alone."'
    )
    attrs = attribute_dialogue(text)
    speakers = [a.speaker for a in attrs]
    # First two are tagged; the last two alternate Elsa/Anna.
    assert speakers[0] == "Elsa"
    assert speakers[1] == "Anna"
    assert speakers[2] == "Elsa"  # alternation: the other party
    assert speakers[3] == "Anna"
    assert attrs[2].method == "alternation"


def test_canon_filter_drops_invented_speaker() -> None:
    # "Mordred" is not in canon → the line is left unattributed, never invented.
    attrs = attribute_dialogue('"Betray them all," said Mordred.', canon_names={"Elsa", "Anna"})
    assert attrs[0].speaker == ""
    # An in-canon speaker is kept.
    attrs2 = attribute_dialogue('"Trust me," said Elsa.', canon_names={"Elsa"})
    assert attrs2[0].speaker == "Elsa"


def test_canon_filter_accepts_mapping() -> None:
    attrs = attribute_dialogue(
        '"Onward," said Elsa.', canon_names={"elsa": "char_elsa"}
    )
    assert attrs[0].speaker == "Elsa"


def test_to_dialogue_lines_projection() -> None:
    attrs = attribute_dialogue('"Hello there" said Elsa. "Good day" Anna replied.')
    lines = to_dialogue_lines(attrs)
    assert [(line.speaker, line.quote) for line in lines] == [
        ("Elsa", "Hello there"),
        ("Anna", "Good day"),
    ]


def test_dialogue_density() -> None:
    none = dialogue_density("The wind howled across the empty plain.")
    assert none == 0.0
    heavy = dialogue_density('"I will never leave you," she swore.')
    assert heavy > 0.4


def test_no_quotes_returns_empty() -> None:
    assert attribute_dialogue("He walked to the door and turned the handle.") == []
