"""Backend-agnostic SyncMap builder (kinora.md §9.4): assemble karaoke spans +
page-turn + per-sentence anchors from ANY provider timing shape, the no-timing
estimator, a duration rescale, and a multi-segment shot spanning N chained clips —
reusing the page-box alignment so word_index/bbox match the reading room. Pure."""

from __future__ import annotations

import pytest

from app.providers.types import TtsWord
from app.video.sync.builder import build_sync_map
from app.video.sync.models import ClipSegment, TimingShape
from app.video.sync.validators import validate_sync_map

_BOXES = [
    {"word_index": 100, "text": "She", "bbox": [0.10, 0.30, 0.04, 0.02]},
    {"word_index": 101, "text": "stood", "bbox": [0.16, 0.30, 0.06, 0.02]},
    {"word_index": 102, "text": "still.", "bbox": [0.24, 0.30, 0.06, 0.02]},
]
_SPAN = {"page": 12, "word_range": [100, 102]}


def _raw() -> list[dict[str, float | str]]:
    return [
        {"text": "She", "t_start": 0.10, "t_end": 0.32},
        {"text": "stood", "t_start": 0.32, "t_end": 0.61},
        {"text": "still", "t_start": 0.61, "t_end": 1.00},
    ]


# --------------------------------------------------------------------------- #
# shape + alignment
# --------------------------------------------------------------------------- #


def test_per_word_aligns_to_page_boxes() -> None:
    m = build_sync_map(
        shot_id="shot_1",
        raw_timings=_raw(),
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
    )
    assert [w.word_index for w in m.words] == [100, 101, 102]
    assert m.words[0].bbox == [0.10, 0.30, 0.04, 0.02]
    # painted text comes from the page box (carries the period)
    assert m.words[2].text == "still."
    assert m.page == 12
    assert not m.estimated
    assert validate_sync_map(m) == []


def test_duration_rescale_locks_last_word_to_clip_end() -> None:
    # narration spans 1.0s but the clip is 5.0s → stretch onto the clip.
    m = build_sync_map(
        shot_id="s",
        raw_timings=_raw(),
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
    )
    assert m.video_end_s == 5.0
    assert m.words[-1].t_end == 5.0


def test_video_start_offsets_onto_scene_playhead() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=_raw(),
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        video_start_s=10.0,
        duration_s=5.0,
    )
    assert m.video_start_s == 10.0
    assert m.video_end_s == 15.0
    assert m.words[0].t_start >= 10.0
    assert m.words[-1].t_end == 15.0
    assert 10.0 <= m.page_turn_at_s < 15.0


def test_estimator_path_when_no_timings() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=None,
        narration_text="She stood still.",
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=4.0,
    )
    assert m.estimated is True
    assert all(w.estimated for w in m.words)
    assert m.video_end_s == 4.0
    assert m.words[-1].t_end == 4.0
    assert validate_sync_map(m) == []


def test_cue_shape_builds_valid_map() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=[{"text": "She stood still", "start": 0.0, "end": 4.0}],
        timing_shape=TimingShape.CUE,
        narration_text="She stood still.",
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=4.0,
    )
    assert [w.word_index for w in m.words] == [100, 101, 102]
    assert validate_sync_map(m) == []


def test_no_page_boxes_indexes_off_span_start() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=_raw(),
        source_span={"page": 7, "word_range": [50, 99]},
        page_word_boxes=None,
        duration_s=3.0,
    )
    assert [w.word_index for w in m.words] == [50, 51, 52]
    assert all(w.bbox is None for w in m.words)
    assert m.page == 7


# --------------------------------------------------------------------------- #
# multi-segment (one shot spanning chained clips)
# --------------------------------------------------------------------------- #


def test_multi_segment_spans_summed_duration() -> None:
    segs = [ClipSegment(clip_id="a", duration_s=3.0), ClipSegment(clip_id="b", duration_s=2.0)]
    m = build_sync_map(
        shot_id="s",
        raw_timings=[
            {"text": "one", "t_start": 0.0, "t_end": 1.0},
            {"text": "two", "t_start": 1.0, "t_end": 2.0},
        ],
        segments=segs,
        duration_s=999.0,  # ignored: segments win
    )
    assert m.video_end_s == 5.0
    assert m.words[-1].t_end == 5.0
    assert validate_sync_map(m) == []


def test_multi_segment_estimator_uses_summed_duration() -> None:
    segs = [ClipSegment(clip_id="a", duration_s=2.0), ClipSegment(clip_id="b", duration_s=2.0)]
    m = build_sync_map(
        shot_id="s",
        raw_timings=None,
        narration_text="alpha beta gamma",
        segments=segs,
    )
    assert m.video_end_s == 4.0
    assert m.words[-1].t_end == 4.0
    assert m.estimated is True


# --------------------------------------------------------------------------- #
# sentence anchors + page-turn
# --------------------------------------------------------------------------- #


def test_sentence_anchors_from_narration_text() -> None:
    boxes = [
        {"word_index": i, "text": t}
        for i, t in enumerate(["She", "ran.", "He", "walked."], start=0)
    ]
    m = build_sync_map(
        shot_id="s",
        raw_timings=[
            {"text": "She", "t_start": 0.0, "t_end": 1.0},
            {"text": "ran", "t_start": 1.0, "t_end": 2.0},
            {"text": "He", "t_start": 2.0, "t_end": 3.0},
            {"text": "walked", "t_start": 3.0, "t_end": 4.0},
        ],
        narration_text="She ran. He walked.",
        source_span={"page": 1, "word_range": [0, 3]},
        page_word_boxes=boxes,
        duration_s=4.0,
    )
    assert len(m.sentences) == 2
    assert m.sentences[0].word_start == 0 and m.sentences[0].word_end == 1
    assert m.sentences[1].word_start == 2 and m.sentences[1].word_end == 3
    # anchor spans match the words they cover
    assert m.sentences[0].t_start == m.words[0].t_start
    assert m.sentences[1].t_end == m.words[3].t_end


def test_sentence_anchors_inferred_when_no_text() -> None:
    boxes = [{"word_index": i, "text": t} for i, t in enumerate(["Hi!", "Bye."])]
    m = build_sync_map(
        shot_id="s",
        raw_timings=[
            {"text": "Hi", "t_start": 0.0, "t_end": 1.0},
            {"text": "Bye", "t_start": 1.0, "t_end": 2.0},
        ],
        source_span={"page": 1, "word_range": [0, 1]},
        page_word_boxes=boxes,
        duration_s=2.0,
    )
    # painted text carries the terminal punctuation from the boxes → 2 sentences
    assert len(m.sentences) == 2


def test_page_turn_before_end() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=_raw(),
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=5.0,
        page_turn_lead_s=0.2,
    )
    assert m.page_turn_at_s == pytest.approx(4.8)
    assert m.video_start_s <= m.page_turn_at_s < m.video_end_s


def test_accepts_ttsword_input() -> None:
    m = build_sync_map(
        shot_id="s",
        raw_timings=[TtsWord(text="She", t_start=0.0, t_end=1.0)],
        source_span=_SPAN,
        page_word_boxes=_BOXES,
        duration_s=2.0,
    )
    assert m.words[0].word_index == 100
    assert m.words[0].t_end == 2.0
