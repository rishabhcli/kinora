"""The §13 metrics-snapshot dashboard assembler over both streams."""

from __future__ import annotations

from app.streaming.processing.pipelines.dashboard import (
    COMMIT_HORIZON_S,
    MetricsSnapshot,
    build_metrics_snapshot,
    crew_vs_baseline_delta,
)
from app.streaming.processing.pipelines.events import (
    IntentKind,
    QAVerdict,
    ReaderIntentEvent,
    RenderEvent,
    RenderEventKind,
)


def _intent(session: str, ts: int, word: int, vel: float) -> ReaderIntentEvent:
    return ReaderIntentEvent(
        session_id=session,
        book_id="b1",
        kind=IntentKind.SETTLE,
        focus_word=word,
        velocity_wps=vel,
        ts_ms=ts,
    )


def _render(
    kind: RenderEventKind,
    ts: int,
    *,
    shot: str = "",
    request: str = "",
    duration: float = 0.0,
    ccs: float | None = None,
    qa: QAVerdict = QAVerdict.NONE,
) -> RenderEvent:
    return RenderEvent(
        session_id="sess",
        book_id="b1",
        kind=kind,
        shot_id=shot,
        request_id=request,
        duration_s=duration,
        ccs=ccs,
        qa=qa,
        ts_ms=ts,
    )


def _sample_render_events() -> list[RenderEvent]:
    return [
        _render(RenderEventKind.RENDER_REQUESTED, 1_000, request="r1", shot="s1"),
        _render(
            RenderEventKind.CLIP_READY,
            1_012,
            request="r1",
            shot="s1",
            duration=5.0,
            ccs=0.92,
            qa=QAVerdict.PASS,
        ),
        _render(RenderEventKind.RENDER_REQUESTED, 2_000, request="r2", shot="s2"),
        _render(
            RenderEventKind.CLIP_READY,
            2_018,
            request="r2",
            shot="s2",
            duration=5.0,
            ccs=0.88,
            qa=QAVerdict.PASS,
        ),
        _render(RenderEventKind.CLIP_READY, 3_000, shot="s3", duration=5.0, qa=QAVerdict.FAIL),
        _render(RenderEventKind.REGEN_DONE, 4_000, shot="s3", duration=5.0, qa=QAVerdict.PASS),
    ]


def test_metrics_snapshot_combines_both_streams() -> None:
    reader = [
        _intent("s1", 1_000, 0, 4.0),
        _intent("s1", 3_000, 30, 4.0),
        _intent("s1", 6_000, 60, 5.0),
    ]
    snap = build_metrics_snapshot(reader, _sample_render_events())
    assert isinstance(snap, MetricsSnapshot)
    assert snap.finished_shots == 4  # 2 clips + 1 fail clip + 1 regen
    assert snap.accepted_shots == 2  # the two PASS clips
    assert snap.regenerations == 2  # fail + regen
    assert snap.accepted_footage_efficiency is not None
    assert snap.mean_ccs is not None and 0.88 <= snap.mean_ccs <= 0.92
    assert snap.p95_latency_ms is not None and snap.p95_latency_ms >= 12
    assert snap.mean_velocity_wps is not None and snap.mean_velocity_wps > 0
    assert snap.reading_sessions >= 1


def test_velocity_adaptive_lookahead_scales_with_velocity() -> None:
    slow = MetricsSnapshot(
        finished_shots=0,
        accepted_shots=0,
        accepted_seconds=0.0,
        regenerations=0,
        accepted_footage_efficiency=None,
        regeneration_rate=None,
        mean_ccs=None,
        p50_latency_ms=None,
        p95_latency_ms=None,
        mean_velocity_wps=3.0,
        reading_sessions=1,
        stalls=0,
    )
    fast = MetricsSnapshot(
        finished_shots=0,
        accepted_shots=0,
        accepted_seconds=0.0,
        regenerations=0,
        accepted_footage_efficiency=None,
        regeneration_rate=None,
        mean_ccs=None,
        p50_latency_ms=None,
        p95_latency_ms=None,
        mean_velocity_wps=9.0,
        reading_sessions=1,
        stalls=0,
    )
    # nominal velocity -> exactly the commit horizon
    assert slow.recommended_lookahead_s() == COMMIT_HORIZON_S
    # faster reader -> larger lookahead (self-tuning §4.6), clamped at 3x
    assert fast.recommended_lookahead_s() > slow.recommended_lookahead_s()
    assert fast.recommended_lookahead_s() <= COMMIT_HORIZON_S * 3.0


def test_crew_beats_baseline_delta() -> None:
    crew = build_metrics_snapshot([], _sample_render_events())
    # a baseline with more regens and lower ccs
    baseline_events = [
        _render(
            RenderEventKind.CLIP_READY, 1_000, shot="s1", duration=5.0, ccs=0.7, qa=QAVerdict.FAIL
        ),
        _render(RenderEventKind.REGEN_DONE, 1_500, shot="s1", duration=5.0, qa=QAVerdict.PASS),
        _render(
            RenderEventKind.CLIP_READY, 2_000, shot="s2", duration=5.0, ccs=0.72, qa=QAVerdict.FAIL
        ),
        _render(RenderEventKind.REGEN_DONE, 2_500, shot="s2", duration=5.0, qa=QAVerdict.PASS),
    ]
    baseline = build_metrics_snapshot([], baseline_events)
    delta = crew_vs_baseline_delta(crew, baseline)
    # the crew rejects less footage -> higher efficiency, lower regen rate
    assert delta["efficiency_gain"] > 0
    assert delta["regen_rate_reduction"] > 0
    assert delta["ccs_gain"] > 0
