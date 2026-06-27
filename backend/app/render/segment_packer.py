"""Segment packer — group consecutive beats into ≤15s reading-synced segments.

The single-clip pipeline (the 15s i2v overhaul) renders one continuous take per
**segment** instead of stitching many ~5s shots. A segment is the longest run of
consecutive, **same-page** beats whose summed reading-paced duration stays within
:data:`MAX_SEGMENT_S` (the wan2.7 ceiling). Packing is pure — the per-beat
duration estimator is injected — so it is unit-testable without ffmpeg or a
network. The Event Director passes its ``shot_duration_for_beat`` and renders
each segment as one clip, falling back to the stitcher only when a scene yields
more than one segment.

Page-bounded packing keeps the single-page sync map (§9.4) unchanged: at the
default ``pages_per_scene=1`` a scene is one page, so segments never cross a page
turn and each carries exactly one page.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.agents.contracts import Beat, Segment, SourceSpan

#: The hard ceiling for one continuous clip — wan2.7 t2v/i2v top out at 15s.
MAX_SEGMENT_S = 15.0

#: Injected per-beat duration estimator (the Event Director's ``shot_duration_for_beat``).
DurationForBeat = Callable[[Beat], float]


def pack_segments(
    beats: Sequence[Beat],
    *,
    duration_for_beat: DurationForBeat,
    scene_id: str | None = None,
    max_segment_s: float = MAX_SEGMENT_S,
) -> list[Segment]:
    """Pack ``beats`` into ordered ≤``max_segment_s`` segments (pure, deterministic).

    Consecutive beats accrete into the current segment until adding the next beat
    would push it past ``max_segment_s`` *or* the beat sits on a new page, at
    which point the segment closes and the beat opens the next one. A lone beat
    whose own estimate exceeds the ceiling becomes its own segment with the
    duration clamped to ``max_segment_s``.
    """
    base = scene_id or (beats[0].scene_id if beats else None) or "scene"
    segments: list[Segment] = []
    bucket: list[Beat] = []
    bucket_dur = 0.0
    bucket_page: int | None = None

    def flush() -> None:
        nonlocal bucket, bucket_dur, bucket_page
        if not bucket:
            return
        ordinal = len(segments)
        starts = [b.source_span.word_range[0] for b in bucket]
        ends = [b.source_span.word_range[1] for b in bucket]
        segments.append(
            Segment(
                segment_id=f"{base}_seg_{ordinal:02d}",
                ordinal=ordinal,
                beat_ids=[b.beat_id for b in bucket],
                source_span=SourceSpan(
                    page=bucket_page or 0,
                    para=bucket[0].source_span.para,
                    word_range=(min(starts), max(ends)),
                ),
                duration_s=round(min(bucket_dur, max_segment_s), 2),
            )
        )
        bucket = []
        bucket_dur = 0.0
        bucket_page = None

    for beat in beats:
        dur = float(duration_for_beat(beat))
        page = beat.source_span.page
        if bucket and (page != bucket_page or bucket_dur + dur > max_segment_s):
            flush()
        if not bucket:
            bucket_page = page
        bucket.append(beat)
        bucket_dur += dur
    flush()
    return segments


__all__ = ["MAX_SEGMENT_S", "DurationForBeat", "Segment", "pack_segments"]
