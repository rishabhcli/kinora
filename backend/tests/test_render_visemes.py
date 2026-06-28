"""Viseme track from the phoneme sync map (kinora.md §9.4): deterministic
grapheme→viseme mapping, per-word frames, segment track with rest fills, and
coalescing of adjacent identical shapes. Pure — no model, no audio."""

from __future__ import annotations

from app.providers.types import TtsWord
from app.render.sync_map import SyncSegment, SyncWord, build_sync_segment
from app.render.visemes import (
    Viseme,
    segment_visemes,
    viseme_for_chunk,
    word_visemes,
)

_BOXES = [
    {"word_index": 100, "text": "She", "bbox": None},
    {"word_index": 101, "text": "stood", "bbox": None},
]
_SPAN = {"page": 1, "word_range": [100, 101]}


def test_viseme_for_chunk_consonants_and_vowels() -> None:
    assert viseme_for_chunk("p") is Viseme.PP
    assert viseme_for_chunk("ba") is Viseme.PP  # leading b
    assert viseme_for_chunk("fo") is Viseme.FF
    assert viseme_for_chunk("oo") is Viseme.OH
    assert viseme_for_chunk("") is Viseme.SIL


def test_viseme_for_chunk_digraphs() -> None:
    assert viseme_for_chunk("th") is Viseme.TH
    assert viseme_for_chunk("shoe") is Viseme.CH  # "sh" digraph
    assert viseme_for_chunk("photo") is Viseme.FF  # "ph"
    assert viseme_for_chunk("quay") is Viseme.KK  # "qu"


def test_word_visemes_without_phonemes_one_frame() -> None:
    word = SyncWord(word_index=1, text="Mother", t_start=1.0, t_end=2.0)
    frames = word_visemes(word)
    assert len(frames) == 1
    assert frames[0].viseme is Viseme.PP  # leading m
    assert frames[0].t_start == 1.0 and frames[0].t_end == 2.0


def test_word_visemes_from_phonemes() -> None:
    narrated = [TtsWord(text="water", t_start=0.0, t_end=1.0)]
    seg = build_sync_segment(
        shot_id="s",
        word_timestamps=narrated,
        source_span={"page": 1, "word_range": [0, 0]},
        page_word_boxes=None,
        duration_s=1.0,
        phoneme_timing=True,
    )
    frames = word_visemes(seg.words[0])
    # "water" → ["wa","ter"] → OU then DD.
    assert [f.viseme for f in frames] == [Viseme.OU, Viseme.DD]
    assert frames[0].t_start == 0.0
    assert frames[-1].t_end == 1.0


def test_segment_visemes_fills_rests_between_words() -> None:
    words = [
        SyncWord(word_index=1, text="She", t_start=0.5, t_end=1.0),
        SyncWord(word_index=2, text="stood", t_start=1.5, t_end=2.0),
    ]
    seg = SyncSegment(
        shot_id="s", video_start_s=0.0, video_end_s=3.0, page=1, page_turn_at_s=2.8, words=words
    )
    frames = segment_visemes(seg)
    # Lead-in rest (0→0.5), word, gap rest (1.0→1.5), word, tail rest (2.0→3.0).
    assert frames[0].viseme is Viseme.SIL and frames[0].t_start == 0.0
    assert frames[-1].viseme is Viseme.SIL and frames[-1].t_end == 3.0
    # Continuous: every frame's end is the next frame's start.
    for a, b in zip(frames, frames[1:], strict=False):
        assert abs(a.t_end - b.t_start) < 1e-3


def test_segment_visemes_no_rests_when_disabled() -> None:
    words = [SyncWord(word_index=1, text="She", t_start=0.5, t_end=1.0)]
    seg = SyncSegment(
        shot_id="s", video_start_s=0.0, video_end_s=2.0, page=1, page_turn_at_s=1.8, words=words
    )
    frames = segment_visemes(seg, with_rests=False)
    assert all(f.viseme is not Viseme.SIL for f in frames)


def test_segment_visemes_coalesces_identical_runs() -> None:
    # Two adjacent words both leading with "m" + no gap → PP runs merge.
    words = [
        SyncWord(word_index=1, text="my", t_start=0.0, t_end=0.5),
        SyncWord(word_index=2, text="mother", t_start=0.5, t_end=1.0),
    ]
    seg = SyncSegment(
        shot_id="s", video_start_s=0.0, video_end_s=1.0, page=1, page_turn_at_s=0.9, words=words
    )
    frames = segment_visemes(seg, with_rests=False)
    assert len(frames) == 1  # merged into one PP span
    assert frames[0].viseme is Viseme.PP
    assert frames[0].t_start == 0.0 and frames[0].t_end == 1.0
