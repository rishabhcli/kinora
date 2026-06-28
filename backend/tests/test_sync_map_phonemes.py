"""Phoneme sub-timing in the sync map (kinora.md §9.4 richer sync): deterministic
grapheme chunking, phoneme distribution anchored to the real word timing, the
opt-in builder flag, backwards compatibility, and that phonemes ride the cumulative
shift through a stitch merge. Pure — no ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.providers.types import TtsWord
from app.render.stitch import merge_sync_segments
from app.render.sync_map import (
    SyncSegment,
    SyncWord,
    build_sync_segment,
    grapheme_chunks,
    split_phonemes,
)

_BOXES = [
    {"word_index": 100, "text": "She", "bbox": [0.10, 0.30, 0.04, 0.02]},
    {"word_index": 101, "text": "stood", "bbox": [0.16, 0.30, 0.06, 0.02]},
    {"word_index": 102, "text": "still", "bbox": [0.24, 0.30, 0.06, 0.02]},
]
_SPAN = {"page": 12, "word_range": [100, 102]}


# --------------------------------------------------------------------------- #
# grapheme_chunks
# --------------------------------------------------------------------------- #


def test_grapheme_chunks_syllable_like_split() -> None:
    # Monosyllables stay one chunk (the coda attaches to the nucleus' onset).
    assert grapheme_chunks("stood") == ["stood"]
    assert grapheme_chunks("a") == ["a"]
    # Multi-syllable words split at each new onset+nucleus.
    assert grapheme_chunks("meadow") == ["mea", "dow"]
    assert grapheme_chunks("water") == ["wa", "ter"]


def test_grapheme_chunks_strips_punctuation() -> None:
    assert grapheme_chunks('"Stop!"') == grapheme_chunks("stop")
    assert grapheme_chunks("!!!") == []
    assert grapheme_chunks("") == []


def test_grapheme_chunks_trailing_consonants_attach() -> None:
    # No vowel-less standalone chunk is ever emitted.
    for word in ("strength", "rhythm", "world"):
        chunks = grapheme_chunks(word)
        assert all(any(c in "aeiouy" for c in chunk) or i == 0 for i, chunk in enumerate(chunks))


# --------------------------------------------------------------------------- #
# split_phonemes
# --------------------------------------------------------------------------- #


def test_split_phonemes_spans_sum_to_word() -> None:
    phonemes = split_phonemes("meadow", 1.0, 2.0)
    assert len(phonemes) == 2
    assert phonemes[0].t_start == 1.0
    assert phonemes[-1].t_end == 2.0  # snaps exactly to the word end
    # Monotonic, contiguous.
    assert phonemes[0].t_end == phonemes[1].t_start


def test_split_phonemes_weighted_by_chunk_length() -> None:
    # "strength" (1 chunk) vs the multi-chunk weighting: use a word whose chunks
    # differ in length. "into" → "i" (1) + "nto" (3): the longer chunk gets more.
    phonemes = split_phonemes("into", 0.0, 4.0)
    assert len(phonemes) == 2
    short = phonemes[0].t_end - phonemes[0].t_start  # "i"
    long = phonemes[1].t_end - phonemes[1].t_start  # "nto"
    assert long > short


def test_split_phonemes_monosyllable_is_one_chunk() -> None:
    phonemes = split_phonemes("stood", 1.0, 2.0)
    assert len(phonemes) == 1
    assert phonemes[0].t_start == 1.0 and phonemes[0].t_end == 2.0


def test_split_phonemes_zero_duration_is_empty() -> None:
    assert split_phonemes("stood", 2.0, 2.0) == []
    assert split_phonemes("stood", 3.0, 1.0) == []
    assert split_phonemes("!!!", 0.0, 1.0) == []


# --------------------------------------------------------------------------- #
# Builder integration
# --------------------------------------------------------------------------- #


def test_build_segment_without_phonemes_is_backwards_compatible() -> None:
    narrated = [TtsWord(text="stood", t_start=0.0, t_end=2.0)]
    seg = build_sync_segment(
        shot_id="s",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
    )
    # Default: no phonemes (the optional field defaults empty).
    assert seg.words[0].phonemes == []


def test_build_segment_with_phoneme_timing() -> None:
    narrated = [
        TtsWord(text="She", t_start=0.10, t_end=0.40),
        TtsWord(text="stood", t_start=0.40, t_end=1.20),
    ]
    seg = build_sync_segment(
        shot_id="s",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
        phoneme_timing=True,
    )
    stood = seg.words[1]
    assert len(stood.phonemes) >= 1
    # Phonemes are absolute on the playhead and bracketed by the word.
    assert stood.phonemes[0].t_start == pytest.approx(stood.t_start)
    assert stood.phonemes[-1].t_end == pytest.approx(stood.t_end)


def test_phonemes_respect_video_start_offset() -> None:
    narrated = [TtsWord(text="stood", t_start=0.0, t_end=1.0)]
    seg = build_sync_segment(
        shot_id="s",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        video_start_s=10.0,
        duration_s=2.0,
        phoneme_timing=True,
    )
    # Phonemes start at the shot's playhead offset, not at 0.
    assert seg.words[0].phonemes[0].t_start == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# Merge shifts phonemes in lockstep with the word
# --------------------------------------------------------------------------- #


def test_merge_shifts_phonemes_with_cumulative_offset() -> None:
    def seg(shot_id: str) -> SyncSegment:
        narrated = [TtsWord(text="stood", t_start=0.2, t_end=0.9)]
        return build_sync_segment(
            shot_id=shot_id,
            word_timestamps=narrated,
            source_span=_SPAN,
            page_word_boxes=_BOXES,
            duration_s=1.0,
            phoneme_timing=True,
        )

    merged = merge_sync_segments([seg("a"), seg("b")], scene_id="sc", durations=[1.0, 1.0])
    second_word = merged.segments[1].words[0]
    # Word shifted by the first shot's 1.0s — and so are its phonemes.
    assert second_word.t_start == pytest.approx(1.2)
    assert second_word.phonemes[0].t_start == pytest.approx(1.2)
    assert second_word.phonemes[-1].t_end == pytest.approx(second_word.t_end)


def test_merge_preserves_empty_phonemes() -> None:
    word = SyncWord(word_index=1, text="hi", t_start=0.1, t_end=0.5, bbox=None)
    seg = SyncSegment(
        shot_id="a", video_start_s=0.0, video_end_s=1.0, page=1, page_turn_at_s=0.8, words=[word]
    )
    merged = merge_sync_segments([seg, seg], scene_id="sc", durations=[1.0, 1.0])
    assert merged.segments[1].words[0].phonemes == []  # stays empty, no crash
