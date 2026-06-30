"""Audio→video retiming (kinora.md §9.4): rescale audio-clock word timings onto the
actual rendered video duration — single clip and the multi-segment chained-clip
case where one logical shot spans N provider clips. Pure: no ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.providers.types import TtsWord
from app.video.sync.models import ClipSegment, WordTiming
from app.video.sync.retime import (
    rescale_across_segments,
    rescale_to_duration,
    segment_boundaries,
    segment_index_at,
    total_segment_duration,
)

_WORDS = [
    WordTiming(text="She", t_start=0.0, t_end=1.0),
    WordTiming(text="stood", t_start=1.0, t_end=2.0),
]


# --------------------------------------------------------------------------- #
# single-clip rescale
# --------------------------------------------------------------------------- #


def test_rescale_stretches_short_narration() -> None:
    out = rescale_to_duration(_WORDS, target_duration_s=5.0)  # 2s → 5s, ×2.5
    assert out[0].t_start == 0.0
    assert out[-1].t_end == 5.0
    assert out[0].t_end == pytest.approx(2.5)


def test_rescale_compresses_long_narration() -> None:
    out = rescale_to_duration(_WORDS, target_duration_s=1.0)  # 2s → 1s, ×0.5
    assert out[-1].t_end == pytest.approx(1.0)
    assert out[0].t_end == pytest.approx(0.5)


def test_rescale_preserves_relative_spacing() -> None:
    words = [
        WordTiming(text="a", t_start=0.0, t_end=0.5),
        WordTiming(text="b", t_start=0.5, t_end=2.0),  # 3× longer
    ]
    out = rescale_to_duration(words, target_duration_s=4.0)
    span_a = out[0].duration
    span_b = out[1].duration
    assert span_b / span_a == pytest.approx(3.0)


def test_rescale_accepts_ttsword_objects() -> None:
    out = rescale_to_duration([TtsWord(text="x", t_start=0.0, t_end=2.0)], target_duration_s=4.0)
    assert out[0].t_end == 4.0


def test_rescale_carries_estimated_flag() -> None:
    words = [WordTiming(text="x", t_start=0.0, t_end=1.0, estimated=True)]
    out = rescale_to_duration(words, target_duration_s=2.0)
    assert out[0].estimated is True


def test_rescale_noop_for_empty_or_zero_span() -> None:
    assert rescale_to_duration([], target_duration_s=5.0) == []
    zero = [WordTiming(text="x", t_start=0.0, t_end=0.0)]
    assert rescale_to_duration(zero, target_duration_s=5.0) == zero
    # non-positive target → unchanged
    assert rescale_to_duration(_WORDS, target_duration_s=0.0) == _WORDS


# --------------------------------------------------------------------------- #
# multi-segment (chained provider clips)
# --------------------------------------------------------------------------- #

_SEGS = [ClipSegment(clip_id="a", duration_s=3.0), ClipSegment(clip_id="b", duration_s=2.0)]


def test_total_segment_duration_sums() -> None:
    assert total_segment_duration(_SEGS) == 5.0


def test_rescale_across_segments_uses_summed_duration() -> None:
    # narration spans 2s; chained clips sum to 5s → stretch onto 5s, seam invisible.
    out = rescale_across_segments(_WORDS, _SEGS)
    assert out[-1].t_end == 5.0
    assert out[0].t_end == pytest.approx(2.5)


def test_word_straddling_a_seam_keeps_one_continuous_span() -> None:
    # A single word whose retimed span crosses the 3.0s seam stays one span.
    words = [WordTiming(text="long", t_start=0.0, t_end=2.0)]
    out = rescale_across_segments(words, _SEGS)
    assert out[0].t_start == 0.0
    assert out[0].t_end == 5.0  # spans both clips as one continuous word


def test_rescale_across_no_segments_is_noop() -> None:
    assert rescale_across_segments(_WORDS, []) == _WORDS


def test_segment_boundaries_are_cumulative() -> None:
    assert segment_boundaries(_SEGS) == [0.0, 3.0, 5.0]


@pytest.mark.parametrize(
    ("t", "expected"),
    [(0.0, 0), (1.5, 0), (3.0, 1), (4.9, 1), (5.0, 1), (99.0, 1)],
)
def test_segment_index_at(t: float, expected: int) -> None:
    assert segment_index_at(segment_boundaries(_SEGS), t) == expected


def test_segment_index_single_clip() -> None:
    bounds = segment_boundaries([ClipSegment(clip_id="solo", duration_s=4.0)])
    assert segment_index_at(bounds, 2.0) == 0
