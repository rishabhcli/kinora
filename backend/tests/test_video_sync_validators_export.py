"""Sync-map validators + WebVTT/SRT export (kinora.md §9.4): the timeline must be
monotonic, non-overlapping, within the clip, and cover every word; and a valid map
exports to the universal subtitle formats. Pure: no ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.video.sync.builder import build_sync_map
from app.video.sync.export import to_srt, to_webvtt
from app.video.sync.models import SyncMap, SyncSentence, SyncWord, WordTiming
from app.video.sync.validators import (
    assert_valid_sync_map,
    check_coverage,
    check_monotonic,
    check_within_duration,
    validate_sync_map,
    validate_word_timeline,
)


def _good_words() -> list[WordTiming]:
    return [
        WordTiming(text="She", t_start=0.0, t_end=1.0),
        WordTiming(text="ran", t_start=1.0, t_end=2.0),
    ]


# --------------------------------------------------------------------------- #
# word-timeline checks
# --------------------------------------------------------------------------- #


def test_monotonic_passes_for_contiguous() -> None:
    assert check_monotonic(_good_words()) == []


def test_monotonic_flags_overlap() -> None:
    bad = [
        WordTiming(text="a", t_start=0.0, t_end=1.0),
        WordTiming(text="b", t_start=0.5, t_end=1.5),  # overlaps a
    ]
    problems = check_monotonic(bad)
    assert any("overlaps" in p for p in problems)


def test_monotonic_flags_reversed_word() -> None:
    bad = [WordTiming(text="a", t_start=1.0, t_end=0.5)]
    assert any("t_end" in p for p in check_monotonic(bad))


def test_within_duration_flags_overrun() -> None:
    words = [WordTiming(text="a", t_start=0.0, t_end=6.0)]
    assert any("> duration" in p for p in check_within_duration(words, duration_s=5.0))


def test_within_duration_flags_negative_start() -> None:
    words = [WordTiming(text="a", t_start=-0.5, t_end=1.0)]
    assert any("< 0" in p for p in check_within_duration(words, duration_s=5.0))


def test_coverage_flags_lost_word() -> None:
    assert check_coverage(_good_words(), expected_count=3) != []
    assert check_coverage(_good_words(), expected_count=2) == []


def test_validate_word_timeline_aggregates() -> None:
    assert validate_word_timeline(_good_words(), duration_s=2.0, expected_count=2) == []


# --------------------------------------------------------------------------- #
# whole-map validation
# --------------------------------------------------------------------------- #


def test_built_map_is_valid() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=[
            {"text": "She", "t_start": 0.0, "t_end": 1.0},
            {"text": "ran", "t_start": 1.0, "t_end": 2.0},
        ],
        narration_text="She ran.",
        source_span={"page": 1, "word_range": [0, 1]},
        page_word_boxes=[{"word_index": 0, "text": "She"}, {"word_index": 1, "text": "ran."}],
        duration_s=4.0,
    )
    assert validate_sync_map(m) == []
    assert_valid_sync_map(m)  # does not raise


def test_validate_flags_page_turn_after_end() -> None:
    m = SyncMap(
        shot_id="s",
        video_start_s=0.0,
        video_end_s=5.0,
        page=1,
        page_turn_at_s=6.0,  # after the end
        words=_to_syncwords(_good_words()),
    )
    assert any("page_turn" in p for p in validate_sync_map(m))


def test_validate_flags_out_of_range_sentence() -> None:
    m = SyncMap(
        shot_id="s",
        video_start_s=0.0,
        video_end_s=2.0,
        page=1,
        page_turn_at_s=1.8,
        words=_to_syncwords(_good_words()),
        sentences=[SyncSentence(text="x", t_start=0.0, t_end=1.0, word_start=0, word_end=9)],
    )
    assert any("out of range" in p for p in validate_sync_map(m))


def test_assert_valid_raises_with_all_problems() -> None:
    m = SyncMap(
        shot_id="s",
        video_start_s=0.0,
        video_end_s=1.0,
        page=1,
        page_turn_at_s=2.0,
        words=[SyncWord(word_index=0, text="x", t_start=0.0, t_end=5.0)],
    )
    with pytest.raises(ValueError, match="invalid sync map"):
        assert_valid_sync_map(m)


def _to_syncwords(words: list[WordTiming]) -> list[SyncWord]:
    return [
        SyncWord(word_index=i, text=w.text, t_start=w.t_start, t_end=w.t_end)
        for i, w in enumerate(words)
    ]


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def _map() -> SyncMap:
    return build_sync_map(
        shot_id="s",
        raw_timings=[
            {"text": "She", "t_start": 0.0, "t_end": 1.0},
            {"text": "ran", "t_start": 1.0, "t_end": 2.0},
        ],
        narration_text="She ran.",
        source_span={"page": 1, "word_range": [0, 1]},
        page_word_boxes=[{"word_index": 0, "text": "She"}, {"word_index": 1, "text": "ran."}],
        duration_s=4.0,
    )


def test_webvtt_header_and_inline_word_marks() -> None:
    vtt = to_webvtt(_map())
    assert vtt.startswith("WEBVTT")
    assert "-->" in vtt
    assert "<00:00:00.000>" in vtt  # inline per-word timestamp


def test_webvtt_per_word_mode() -> None:
    vtt = to_webvtt(_map(), per_word=True)
    # two word cues, no inline marks
    assert vtt.count("-->") == 2
    assert "<00:" not in vtt


def test_srt_is_one_indexed_and_uses_comma() -> None:
    srt = to_srt(_map())
    assert srt.startswith("1\n")
    assert "00:00:00,000 --> 00:00:04,000" in srt


def test_srt_per_word_mode_has_two_cues() -> None:
    srt = to_srt(_map(), per_word=True)
    assert "1\n" in srt and "2\n" in srt


def test_timestamp_formatting_hours() -> None:
    m = SyncMap(
        shot_id="s",
        video_start_s=0.0,
        video_end_s=3700.0,
        page=1,
        page_turn_at_s=3699.0,
        words=[SyncWord(word_index=0, text="late", t_start=3661.5, t_end=3661.75)],
    )
    vtt = to_webvtt(m, per_word=True)
    assert "01:01:01.500" in vtt
    srt = to_srt(m, per_word=True)
    assert "01:01:01,500" in srt
