"""Forced-alignment estimator (kinora.md §9.4 proportional fallback) for backends
that report no word timings: distribute words across the measured clip duration by
syllable + punctuation weight. Pure: no ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.video.sync.estimator import estimate_word_timings
from app.video.sync.text import syllable_count, trailing_pause_weight, word_weight

# --------------------------------------------------------------------------- #
# syllable / weight heuristics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("a", 1),
        ("the", 1),
        ("stone", 1),  # silent trailing e
        ("meadow", 2),
        ("beautiful", 3),
        ("rhythm", 1),  # no written vowel-group besides y → floor of 1
        (",", 0),  # punctuation-only
    ],
)
def test_syllable_count(word: str, expected: int) -> None:
    assert syllable_count(word) == expected


def test_trailing_pause_weight_orders_period_above_comma() -> None:
    assert trailing_pause_weight("end.") > trailing_pause_weight("clause,") > 0.0
    assert trailing_pause_weight("plain") == 0.0


def test_word_weight_drops_pause_when_no_gap_after() -> None:
    with_gap = word_weight("end.", gap_after=True)
    without = word_weight("end.", gap_after=False)
    assert with_gap > without


# --------------------------------------------------------------------------- #
# distribution
# --------------------------------------------------------------------------- #


def test_estimate_fills_full_duration_contiguously() -> None:
    out = estimate_word_timings("She stood still", duration_s=3.0)
    assert [w.text for w in out] == ["She", "stood", "still"]
    assert out[0].t_start == 0.0
    assert out[-1].t_end == 3.0  # last word ends exactly at the clip end
    for a, b in zip(out, out[1:], strict=False):
        assert a.t_end == pytest.approx(b.t_start)  # contiguous
    assert all(w.estimated for w in out)


def test_estimate_gives_longer_words_more_time() -> None:
    out = estimate_word_timings("I understanding", duration_s=4.0)
    short, long = out[0].duration, out[1].duration
    assert long > short  # 4-syllable word vs 1-syllable word


def test_estimate_lingers_after_a_period() -> None:
    # The word before the period should be longer than an equivalent word with no
    # following pause, because the pause folds into it.
    paused = estimate_word_timings("go. go", duration_s=3.0)
    flat = estimate_word_timings("go go go", duration_s=3.0)
    assert paused[0].duration > flat[0].duration


def test_estimate_respects_lead_in() -> None:
    out = estimate_word_timings("hello world", duration_s=4.0, lead_in_s=1.0)
    assert out[0].t_start == 1.0
    assert out[-1].t_end == 4.0


def test_estimate_empty_or_nonpositive_returns_empty() -> None:
    assert estimate_word_timings("", duration_s=5.0) == []
    assert estimate_word_timings("hi", duration_s=0.0) == []
    assert estimate_word_timings("hi", duration_s=2.0, lead_in_s=2.0) == []


def test_estimate_is_deterministic() -> None:
    a = estimate_word_timings("the quick brown fox", duration_s=5.0)
    b = estimate_word_timings("the quick brown fox", duration_s=5.0)
    assert [(w.text, w.t_start, w.t_end) for w in a] == [(w.text, w.t_start, w.t_end) for w in b]
