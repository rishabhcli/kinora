"""Unit tests for the word-timestamp normalizer + forced-alignment FALLBACK.

Pure functions only — no network, no model, no spend. Covers the messy-input
normalizer (ms detection, de-overlap, clamping, sorting) and the proportional
estimate that keeps karaoke working for backends with no inline timing.
"""

from __future__ import annotations

import pytest

from app.audio.alignment import (
    align_words,
    estimate_alignment,
    normalize_words,
    tokenize,
)
from app.audio.types import AlignmentMethod, AudioWord


def test_tokenize_splits_on_whitespace() -> None:
    assert tokenize("She  stood\tstill") == ["She", "stood", "still"]
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_normalize_drops_blank_tokens() -> None:
    words = normalize_words([("hi", 0.0, 0.5), ("", 0.5, 0.6), ("  ", 0.6, 0.7)])
    assert [w.text for w in words] == ["hi"]


def test_normalize_detects_milliseconds() -> None:
    # Largest end-time is 1200 -> clearly ms; should scale to seconds.
    words = normalize_words([("a", 100.0, 600.0), ("b", 600.0, 1200.0)])
    assert words[0].t_start == pytest.approx(0.1)
    assert words[0].t_end == pytest.approx(0.6)
    assert words[1].t_end == pytest.approx(1.2)


def test_normalize_keeps_seconds_when_small() -> None:
    # Max end 0.9 -> seconds, unscaled.
    words = normalize_words([("a", 0.1, 0.4), ("b", 0.4, 0.9)])
    assert words[1].t_end == pytest.approx(0.9)


def test_normalize_sorts_and_de_overlaps() -> None:
    # Out of order + overlapping: must come back sorted and monotonic.
    words = normalize_words(
        [("c", 0.8, 1.0), ("a", 0.0, 0.5), ("b", 0.3, 0.7)],
    )
    assert [w.text for w in words] == ["a", "b", "c"]
    # b starts no earlier than a's end; c after b.
    assert words[1].t_start >= words[0].t_end
    assert words[2].t_start >= words[1].t_end


def test_normalize_clamps_to_duration() -> None:
    words = normalize_words([("a", -1.0, 3.0), ("b", 3.0, 9.0)], duration_s=4.0)
    assert words[0].t_start == 0.0
    assert all(w.t_end <= 4.0 for w in words)


def test_normalize_accepts_audioword_inputs() -> None:
    src = [AudioWord(text="x", t_start=0.0, t_end=0.5)]
    out = normalize_words(src)
    assert out[0].text == "x"


def test_normalize_empty_returns_empty() -> None:
    assert normalize_words([]) == ()


def test_estimate_distributes_duration_by_token_length() -> None:
    words = estimate_alignment("a longword", 2.0)
    assert len(words) == 2
    # First word ("a") shorter than "longword" -> shorter span.
    assert words[0].duration < words[1].duration
    # Anchored to the real duration.
    assert words[-1].t_end <= 2.0 + 1e-6
    assert words[0].t_start == 0.0


def test_estimate_has_inter_word_gap() -> None:
    words = estimate_alignment("one two three", 3.0)
    # Each next word starts at-or-after the prior end (a small gap may sit between).
    for prev, nxt in zip(words, words[1:], strict=False):
        assert nxt.t_start >= prev.t_end


def test_estimate_empty_inputs() -> None:
    assert estimate_alignment("", 1.0) == ()
    assert estimate_alignment("word", 0.0) == ()
    assert estimate_alignment("word", -1.0) == ()


def test_align_prefers_model_words() -> None:
    words, method = align_words(
        "she stood",
        1.0,
        model_words=[("she", 0.0, 0.4), ("stood", 0.4, 0.9)],
        method=AlignmentMethod.MODEL,
    )
    assert method is AlignmentMethod.MODEL
    assert [w.text for w in words] == ["she", "stood"]


def test_align_falls_back_when_no_model_words() -> None:
    words, method = align_words("she stood still", 1.5, model_words=None)
    assert method is AlignmentMethod.PROPORTIONAL
    assert len(words) == 3


def test_align_falls_back_on_word_count_mismatch_when_required() -> None:
    # Model returned only 1 word for a 3-word utterance -> unreliable -> estimate.
    words, method = align_words(
        "she stood still",
        1.5,
        model_words=[("she", 0.0, 1.5)],
        require_word_count_match=True,
    )
    assert method is AlignmentMethod.PROPORTIONAL
    assert len(words) == 3


def test_align_keeps_model_when_count_matches() -> None:
    words, method = align_words(
        "she stood",
        1.0,
        model_words=[("she", 0.0, 0.4), ("stood", 0.4, 0.9)],
        require_word_count_match=True,
        method=AlignmentMethod.ASR,
    )
    assert method is AlignmentMethod.ASR
    assert len(words) == 2


def test_align_nothing_to_align_is_none() -> None:
    words, method = align_words("", 1.0, model_words=None)
    assert words == ()
    assert method is AlignmentMethod.NONE
