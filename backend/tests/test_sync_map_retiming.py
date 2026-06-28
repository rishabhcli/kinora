"""Narration↔clip retiming (kinora.md §9.4): linearly rescale narrated word
timings so karaoke stays locked to the rendered clip's length even when the TTS
audio is longer/shorter than the clip. Pure — no ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.providers.types import TtsWord
from app.render.sync_map import build_sync_segment, rescale_word_timings

_WORDS = [
    TtsWord(text="She", t_start=0.0, t_end=1.0),
    TtsWord(text="stood", t_start=1.0, t_end=2.0),
]


def test_rescale_stretches_short_narration_to_clip() -> None:
    # Narration spans 2s; the clip is 5s → stretch by 2.5×.
    out = rescale_word_timings(_WORDS, target_duration_s=5.0)
    assert out[0].t_start == 0.0
    assert out[-1].t_end == 5.0  # last word ends exactly at the clip end
    assert out[0].t_end == pytest.approx(2.5)


def test_rescale_compresses_long_narration() -> None:
    # Narration spans 2s; clip is 1s → compress by 0.5×.
    out = rescale_word_timings(_WORDS, target_duration_s=1.0)
    assert out[-1].t_end == pytest.approx(1.0)
    assert out[0].t_end == pytest.approx(0.5)


def test_rescale_preserves_relative_spacing() -> None:
    words = [
        TtsWord(text="a", t_start=0.0, t_end=0.5),
        TtsWord(text="b", t_start=0.5, t_end=2.0),  # 3× longer than "a"
    ]
    out = rescale_word_timings(words, target_duration_s=4.0)
    span_a = out[0].t_end - out[0].t_start
    span_b = out[1].t_end - out[1].t_start
    assert span_b == pytest.approx(span_a * 3, rel=0.01)  # ratio preserved


def test_rescale_empty_or_zero_is_safe() -> None:
    assert rescale_word_timings([], target_duration_s=5.0) == []
    # Zero target → unchanged (nothing to anchor to).
    out = rescale_word_timings(_WORDS, target_duration_s=0.0)
    assert out[-1].t_end == 2.0


def test_rescaled_words_feed_build_sync_segment() -> None:
    # The retimed words are accepted directly by the segment builder (same shape).
    retimed = rescale_word_timings(_WORDS, target_duration_s=5.0)
    seg = build_sync_segment(
        shot_id="s",
        word_timestamps=retimed,
        source_span={"page": 1, "word_range": [0, 0]},
        page_word_boxes=None,
        duration_s=5.0,
    )
    assert seg.words[-1].t_end == pytest.approx(5.0)
    assert seg.video_end_s == 5.0
