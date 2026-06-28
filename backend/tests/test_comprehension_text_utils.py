"""Unit tests for the comprehension text primitives (no network).

Sentence segmentation (quotes + abbreviations), quote-span extraction across the
straight/curly/guillemet families, and the strip-quotes / tokenizer helpers.
"""

from __future__ import annotations

from app.agents.comprehension.text_utils import (
    extract_quotes,
    split_sentences,
    strip_quotes,
    titlecase_names,
    words,
)


def test_split_sentences_basic_offsets() -> None:
    text = "The sun rose. She woke. The day began."
    sents = split_sentences(text)
    assert [s.text for s in sents] == ["The sun rose.", "She woke.", "The day began."]
    # Offsets recover the exact source slice.
    for s in sents:
        assert text[s.start : s.end] == s.text.strip()
    assert [s.index for s in sents] == [0, 1, 2]


def test_split_sentences_respects_abbreviations() -> None:
    text = "Mr. Darcy bowed. Mrs. Bennet smiled at Dr. Jones."
    sents = split_sentences(text)
    # "Mr.", "Mrs.", "Dr." periods do NOT end a sentence.
    assert len(sents) == 2
    assert sents[0].text == "Mr. Darcy bowed."


def test_split_sentences_keeps_trailing_quote() -> None:
    text = 'She said "go." Then he left.'
    sents = split_sentences(text)
    assert len(sents) == 2
    assert sents[0].text.endswith('"')


def test_extract_quotes_straight_and_curly() -> None:
    text = 'He said "hello there" and she replied “good morning friend”.'
    quotes = extract_quotes(text)
    assert [q.text for q in quotes] == ["hello there", "good morning friend"]
    for q in quotes:
        assert text[q.start] in "\"“"


def test_extract_quotes_ignores_apostrophes() -> None:
    # Curly apostrophes in contractions must not be read as single-quote speech.
    text = "It’s a lovely day and I can’t complain."
    assert extract_quotes(text) == []


def test_extract_quotes_unterminated_runs_to_end() -> None:
    text = 'She whispered "do not look back'
    quotes = extract_quotes(text)
    assert len(quotes) == 1
    assert quotes[0].text == "do not look back"


def test_strip_quotes_leaves_narration_frame() -> None:
    text = '"I will not," she said, "go without you."'
    stripped = strip_quotes(text)
    assert "I will not" not in stripped
    assert "she said" in stripped


def test_words_and_titlecase_names() -> None:
    text = "Elsa ran while Anna watched the storm."
    assert words(text) == ["elsa", "ran", "while", "anna", "watched", "the", "storm"]
    # Title-case proper-name candidates (Elsa, Anna) — sentence-initial words too.
    names = titlecase_names(text)
    assert "Elsa" in names and "Anna" in names
