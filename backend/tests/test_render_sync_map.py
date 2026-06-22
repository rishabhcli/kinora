"""Sync-map alignment (kinora.md §9.4) — pure, no ffmpeg/DB/network.

Covers the two alignment regimes the spec calls out: an exact 1:1 match when the
narrated and source word counts agree, and a proportional fallback when they
differ — both yielding a real ``word_index`` + ``bbox`` per narrated word and a
``page_turn_at_s`` strictly before the shot's end.
"""

from __future__ import annotations

from app.providers.types import TtsWord
from app.render.sync_map import (
    align_words,
    build_sync_segment,
    normalize_token,
    page_turn_at,
)

_BOXES = [
    {"word_index": 100, "text": "She", "bbox": [0.10, 0.30, 0.04, 0.02]},
    {"word_index": 101, "text": "stood", "bbox": [0.16, 0.30, 0.06, 0.02]},
    {"word_index": 102, "text": "still", "bbox": [0.24, 0.30, 0.06, 0.02]},
]
_SPAN = {"page": 12, "word_range": [100, 102]}


def test_exact_match_assigns_word_index_and_bbox() -> None:
    narrated = [
        TtsWord(text="She", t_start=0.10, t_end=0.32),
        TtsWord(text="stood", t_start=0.32, t_end=0.61),
        TtsWord(text="still", t_start=0.61, t_end=0.95),
    ]
    seg = build_sync_segment(
        shot_id="shot_1",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
    )
    assert seg.page == 12
    assert seg.video_start_s == 0.0
    assert seg.video_end_s == 5.0
    assert [w.word_index for w in seg.words] == [100, 101, 102]
    assert seg.words[0].bbox == [0.10, 0.30, 0.04, 0.02]
    assert seg.words[1].t_start == 0.32
    # page turns slightly before the end (the §9.4 example: 4.8 of 5.0).
    assert seg.page_turn_at_s == 4.8
    assert seg.video_start_s <= seg.page_turn_at_s < seg.video_end_s


def test_mismatched_counts_fall_back_to_proportional() -> None:
    # Five narrated words over three source words → proportional spread.
    narrated = [
        TtsWord(text="And", t_start=0.0, t_end=0.2),
        TtsWord(text="she", t_start=0.2, t_end=0.5),
        TtsWord(text="stood", t_start=0.5, t_end=0.9),
        TtsWord(text="there", t_start=0.9, t_end=1.3),
        TtsWord(text="still", t_start=1.3, t_end=1.8),
    ]
    seg = build_sync_segment(
        shot_id="shot_2",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=2.0,
    )
    assert len(seg.words) == 5
    # i*3//5 -> [0, 0, 1, 1, 2]: every narrated word lands on a real page word.
    assert [w.word_index for w in seg.words] == [100, 100, 101, 101, 102]
    assert seg.words[0].bbox == _BOXES[0]["bbox"]
    assert seg.words[4].word_index == 102
    assert seg.video_start_s <= seg.page_turn_at_s < seg.video_end_s


def test_align_words_reports_method() -> None:
    assert align_words(["a", "b", "c"], ["a", "b", "c"]).method == "exact"
    assert align_words(["a", "b", "c", "d"], ["a", "b"]).method == "proportional"
    assert align_words(["a", "b"], []).method == "fallback"


def test_no_page_boxes_indexes_off_span_start() -> None:
    narrated = [TtsWord(text="She", t_start=0.0, t_end=0.4)]
    seg = build_sync_segment(
        shot_id="shot_3",
        word_timestamps=narrated,
        source_span=_SPAN,
        page_word_boxes=None,
        duration_s=1.0,
    )
    assert seg.words[0].word_index == 100  # span start
    assert seg.words[0].bbox is None
    assert seg.words[0].text == "She"


def test_page_turn_strictly_before_end_even_for_short_shots() -> None:
    assert page_turn_at(0.0, 5.0) == 4.8
    short = page_turn_at(0.0, 0.2)
    assert 0.0 <= short < 0.2
    # A zero-length shot collapses page_turn onto the end.
    assert page_turn_at(3.0, 3.0) == 3.0


def test_normalize_token_strips_punctuation_and_case() -> None:
    assert normalize_token("She,") == "she"
    assert normalize_token("STILL.") == "still"
    assert normalize_token("'quote'") == "quote"
