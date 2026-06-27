"""Segment packer — pack consecutive beats into ≤15s reading-synced segments.

The single-clip overhaul replaces the 5s shot with a **segment**: a run of
consecutive beats packed up to ``MAX_SEGMENT_S`` (15s) of reading-paced
screen-time, rendered as ONE continuous i2v take. These tests pin the pure
packing math: duration-budget grouping, the page boundary, the over-long-beat
clamp, and ordering — no ffmpeg, no network.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import Beat, SourceSpan
from app.render.segment_packer import MAX_SEGMENT_S, Segment, pack_segments


def _beat(i: int, *, page: int = 1, start: int = 0) -> Beat:
    return Beat(
        beat_id=f"b{i}",
        scene_id="scene_001",
        beat_index=i,
        summary=f"beat {i} summary text",
        source_span=SourceSpan(page=page, word_range=(start, start + 10)),
    )


def test_pack_segments_groups_beats_until_duration_budget() -> None:
    """Consecutive same-page beats pack into one segment until the next beat
    would push the segment past MAX_SEGMENT_S (15s)."""
    beats = [_beat(i, start=i * 10) for i in range(4)]
    # A flat 5s per beat: three fill a 15s segment exactly; the fourth opens a new one.
    segments = pack_segments(beats, duration_for_beat=lambda b: 5.0, scene_id="scene_001")

    assert isinstance(segments[0], Segment)
    assert [s.beat_ids for s in segments] == [["b0", "b1", "b2"], ["b3"]]
    assert segments[0].duration_s == pytest.approx(15.0)
    assert segments[1].duration_s == pytest.approx(5.0)
    assert [s.ordinal for s in segments] == [0, 1]
    assert MAX_SEGMENT_S == 15.0


def test_pack_segments_splits_on_page_boundary() -> None:
    """A page turn closes the current segment even when the duration budget has
    room — page-bounded packing keeps the single-page sync map valid."""
    beats = [_beat(0, page=1), _beat(1, page=1), _beat(2, page=2)]
    segments = pack_segments(beats, duration_for_beat=lambda b: 5.0, scene_id="s")
    assert [s.beat_ids for s in segments] == [["b0", "b1"], ["b2"]]
    assert [s.source_span.page for s in segments] == [1, 2]


def test_pack_segments_clamps_over_long_single_beat_to_ceiling() -> None:
    """A lone beat whose own estimate exceeds 15s still yields one segment,
    clamped to the ceiling (no clip can exceed MAX_SEGMENT_S)."""
    segments = pack_segments([_beat(0, page=1)], duration_for_beat=lambda b: 40.0, scene_id="s")
    assert len(segments) == 1
    assert segments[0].duration_s == 15.0


def test_pack_segments_empty_returns_no_segments() -> None:
    assert pack_segments([], duration_for_beat=lambda b: 5.0) == []


def test_pack_segments_uses_real_per_beat_durations_and_unions_span() -> None:
    """With the Event Director's real estimator, two short same-page beats pack
    into one segment whose span unions both beats and whose id is scene-scoped."""
    from app.render.event_director import shot_duration_for_beat

    beats = [
        Beat(
            beat_id="b0",
            scene_id="scene_007",
            beat_index=0,
            summary="a calm wide still vista over the water",
            mood="calm",
            source_span=SourceSpan(page=3, word_range=(10, 22)),
        ),
        Beat(
            beat_id="b1",
            scene_id="scene_007",
            beat_index=1,
            summary="she runs",
            mood="frantic chase",
            source_span=SourceSpan(page=3, word_range=(22, 40)),
        ),
    ]
    segments = pack_segments(beats, duration_for_beat=shot_duration_for_beat, scene_id="scene_007")
    assert len(segments) == 1
    seg = segments[0]
    assert seg.segment_id == "scene_007_seg_00"
    assert seg.beat_ids == ["b0", "b1"]
    assert seg.source_span.page == 3
    assert seg.source_span.word_range == (10, 40)
    assert 0 < seg.duration_s <= MAX_SEGMENT_S
