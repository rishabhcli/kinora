"""Property tests for the §4.2 segment packer (``pack_segments``).

The packer groups consecutive same-page beats into ≤15s continuous-take segments.
Its invariants protect the single-page sync map (§9.4) and the wan2.7 duration
ceiling: every beat lands in exactly one segment, in order; no segment crosses a
page; and a segment's packed duration never exceeds the ceiling (modulo a lone
over-length beat, which is clamped). These are exactly the conditions the stitcher
and the sync map downstream assume hold.
"""

from __future__ import annotations

from collections.abc import Callable

from hypothesis import given
from hypothesis import strategies as st

from app.agents.contracts import Beat
from app.render.segment_packer import MAX_SEGMENT_S, Segment, pack_segments
from app.verification.properties.strategies import beat_durations, beat_runs


def _const(duration: float) -> Callable[[Beat], float]:
    return lambda _beat: duration


@st.composite
def runs_with_durations(
    draw: st.DrawFn,
) -> tuple[list[Beat], dict[str, float]]:
    """A beat run plus a per-beat-id duration table (the injected estimator's data)."""
    beats = draw(beat_runs(min_size=0, max_size=12))
    durations = {b.beat_id: draw(beat_durations) for b in beats}
    return beats, durations


def _estimator(durations: dict[str, float]) -> Callable[[Beat], float]:
    return lambda beat: durations[beat.beat_id]


@given(runs_with_durations())
def test_every_beat_appears_exactly_once_in_order(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """Beat conservation: the packed beat ids are the input ids, same order."""
    beats, durations = data
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    packed_ids = [bid for seg in segments for bid in seg.beat_ids]
    assert packed_ids == [b.beat_id for b in beats]


@given(runs_with_durations())
def test_no_segment_crosses_a_page(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """A segment never spans two pages — the single-page sync map stays valid (§9.4)."""
    beats, durations = data
    by_id = {b.beat_id: b for b in beats}
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    for seg in segments:
        pages = {by_id[bid].source_span.page for bid in seg.beat_ids}
        assert len(pages) == 1


@given(runs_with_durations())
def test_packed_duration_respects_ceiling(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """A segment's recorded duration never exceeds the wan2.7 ceiling (§4.2)."""
    beats, durations = data
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    for seg in segments:
        assert seg.duration_s <= MAX_SEGMENT_S + 1e-6


@given(runs_with_durations())
def test_multi_beat_segments_were_within_ceiling_before_clamp(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """A segment with >1 beat only forms when their *summed* estimate fit the ceiling.

    A lone beat may exceed the ceiling (then it's clamped), but the packer never
    glues two beats together past the limit — so any multi-beat segment's raw sum
    is ≤ ceiling.
    """
    beats, durations = data
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    for seg in segments:
        if len(seg.beat_ids) > 1:
            raw_sum = sum(durations[bid] for bid in seg.beat_ids)
            assert raw_sum <= MAX_SEGMENT_S + 1e-6


@given(runs_with_durations())
def test_segments_are_ordinally_indexed(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """Segment ordinals are 0..k-1 in order (the stitcher reassembles by ordinal)."""
    beats, durations = data
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    assert [s.ordinal for s in segments] == list(range(len(segments)))


@given(runs_with_durations())
def test_span_covers_member_beats(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    """A segment's word_range covers every member beat's span (min start, max end)."""
    beats, durations = data
    by_id = {b.beat_id: b for b in beats}
    segments = pack_segments(beats, duration_for_beat=_estimator(durations))
    for seg in segments:
        member_starts = [by_id[bid].source_span.word_range[0] for bid in seg.beat_ids]
        member_ends = [by_id[bid].source_span.word_range[1] for bid in seg.beat_ids]
        assert seg.source_span.word_range[0] == min(member_starts)
        assert seg.source_span.word_range[1] == max(member_ends)


@given(st.integers(min_value=1, max_value=8))
def test_oversized_beats_each_get_their_own_clamped_segment(n: int) -> None:
    """``n`` consecutive over-ceiling same-page beats → ``n`` clamped segments."""
    beats = [
        Beat(beat_id=f"b{i}", beat_index=i, summary="x")
        for i in range(n)
    ]
    # All on page 0 (default), each estimated well over the ceiling.
    segments = pack_segments(beats, duration_for_beat=_const(MAX_SEGMENT_S * 3))
    assert len(segments) == n
    for seg in segments:
        assert len(seg.beat_ids) == 1
        assert seg.duration_s == MAX_SEGMENT_S


def test_empty_input_yields_no_segments() -> None:
    assert pack_segments([], duration_for_beat=_const(5.0)) == []


@given(runs_with_durations())
def test_packing_is_deterministic(
    data: tuple[list[Beat], dict[str, float]],
) -> None:
    beats, durations = data
    a = pack_segments(beats, duration_for_beat=_estimator(durations))
    b = pack_segments(beats, duration_for_beat=_estimator(durations))

    def key(segs: list[Segment]) -> list[tuple[int, tuple[str, ...], float]]:
        return [(s.ordinal, tuple(s.beat_ids), s.duration_s) for s in segs]

    assert key(a) == key(b)
