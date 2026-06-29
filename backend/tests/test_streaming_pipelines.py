"""Pipeline tests: engagement (reader-intent) and render-QA (render-event)."""

from __future__ import annotations

from app.streaming.processing.pipelines.engagement import (
    ReadingSessionSummary,
    ReadingStall,
    build_engagement_pipeline,
    latest_velocity_by_session,
)
from app.streaming.processing.pipelines.events import (
    IntentKind,
    QAVerdict,
    ReaderIntentEvent,
    ReaderMode,
    RenderEvent,
    RenderEventKind,
)
from app.streaming.processing.pipelines.render_qa import (
    LatencyPercentiles,
    QASummary,
    accepted_footage_efficiency,
    build_render_qa_pipeline,
)
from app.streaming.processing.window_operator import WindowResult


# --------------------------------------------------------------------------- #
# Engagement pipeline
# --------------------------------------------------------------------------- #
def _intent(
    session: str,
    ts: int,
    word: int,
    vel: float,
    kind: IntentKind = IntentKind.SETTLE,
) -> ReaderIntentEvent:
    return ReaderIntentEvent(
        session_id=session,
        book_id="b1",
        kind=kind,
        focus_word=word,
        velocity_wps=vel,
        mode=ReaderMode.VIEWER,
        ts_ms=ts,
    )


def test_engagement_velocity_window() -> None:
    events = [
        _intent("s1", 1_000, 10, 4.0),
        _intent("s1", 3_000, 30, 6.0),
        _intent("s1", 6_000, 60, 5.0),
    ]
    result, outputs = build_engagement_pipeline(
        events, velocity_window_ms=10_000, velocity_slide_ms=5_000
    )
    latest = latest_velocity_by_session(result, outputs.velocity_node)
    assert "s1" in latest
    # the mean of the samples falling in the latest window is positive & sane
    assert 4.0 <= latest["s1"] <= 6.0


def test_engagement_session_window_shaping() -> None:
    # two bursts separated by a > 8s gap -> two reading sessions
    events = [
        _intent("s1", 1_000, 0, 4.0),
        _intent("s1", 2_000, 20, 4.0),
        # 10s gap (> 8s session gap) -> new session
        _intent("s1", 12_000, 100, 5.0),
        _intent("s1", 13_000, 130, 5.0),
    ]
    result, outputs = build_engagement_pipeline(events, session_gap_ms=8_000)
    windows = result.typed_values(outputs.session_node, WindowResult)
    summaries = [w.result for w in windows]
    assert len(summaries) == 2
    first: ReadingSessionSummary = summaries[0]
    second: ReadingSessionSummary = summaries[1]
    assert first.words_advanced == 20
    assert second.words_advanced == 30
    assert first.samples == 2 and second.samples == 2


def test_engagement_stall_detection() -> None:
    # forward sample then a long silence -> stall fires after the deadline
    events = [
        _intent("s1", 1_000, 10, 4.0),
        # a far-future event on another session advances the watermark so the
        # s1 stall timer (1000 + 5000 = 6000) fires.
        _intent("s2", 20_000, 0, 4.0),
    ]
    result, outputs = build_engagement_pipeline(events, stall_deadline_ms=5_000)
    stalls = result.typed_values(outputs.stall_node, ReadingStall)
    assert any(s.session_id == "s1" for s in stalls)


def test_engagement_seek_does_not_stall() -> None:
    events = [
        _intent("s1", 1_000, 10, 4.0, kind=IntentKind.SEEK),
        _intent("s2", 20_000, 0, 4.0),
    ]
    result, outputs = build_engagement_pipeline(events, stall_deadline_ms=5_000)
    s1_stalls = [
        s for s in result.typed_values(outputs.stall_node, ReadingStall) if s.session_id == "s1"
    ]
    assert s1_stalls == []


# --------------------------------------------------------------------------- #
# Render-QA pipeline
# --------------------------------------------------------------------------- #
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


def test_render_qa_accept_and_regen_rate() -> None:
    events = [
        _render(RenderEventKind.CLIP_READY, 1_000, shot="s1", duration=5.0, qa=QAVerdict.PASS),
        _render(RenderEventKind.CLIP_READY, 2_000, shot="s2", duration=5.0, qa=QAVerdict.PASS),
        _render(RenderEventKind.CLIP_READY, 3_000, shot="s3", duration=5.0, qa=QAVerdict.FAIL),
        _render(RenderEventKind.REGEN_DONE, 4_000, shot="s3", duration=5.0, qa=QAVerdict.PASS),
    ]
    result, outputs = build_render_qa_pipeline(events, window_ms=60_000)
    rollups = [w.result for w in result.typed_values(outputs.qa_rollup_node, WindowResult)]
    assert len(rollups) == 1
    summary: QASummary = rollups[0]
    assert summary.finished_shots == 4
    # accepted clips: only CLIP_READY with PASS/DEGRADED count (s1, s2)
    assert summary.accepted_shots == 2
    # regenerations: one FAIL clip (s3) + one REGEN_DONE
    assert summary.regenerations == 2


def test_render_qa_throughput_seconds() -> None:
    events = [
        _render(RenderEventKind.CLIP_READY, 1_000, duration=5.0, qa=QAVerdict.PASS),
        _render(RenderEventKind.CLIP_READY, 2_000, duration=8.0, qa=QAVerdict.DEGRADED),
        _render(RenderEventKind.KEYFRAME_READY, 2_500),  # not video-seconds
    ]
    result, outputs = build_render_qa_pipeline(events, window_ms=60_000)
    seconds = [w.result for w in result.typed_values(outputs.throughput_node, WindowResult)]
    assert sum(seconds) == 13.0  # 5 + 8, keyframe excluded


def test_render_qa_ccs_window_mean() -> None:
    events = [
        _render(RenderEventKind.CLIP_READY, 1_000, ccs=0.9, qa=QAVerdict.PASS),
        _render(RenderEventKind.CLIP_READY, 2_000, ccs=0.7, qa=QAVerdict.PASS),
    ]
    result, outputs = build_render_qa_pipeline(events, window_ms=60_000)
    means = [w.result for w in result.typed_values(outputs.ccs_node, WindowResult)]
    assert means and abs(means[0] - 0.8) < 1e-9


def test_render_qa_latency_interval_join_percentiles() -> None:
    events = [
        _render(RenderEventKind.RENDER_REQUESTED, 1_000, request="r1", shot="s1"),
        _render(
            RenderEventKind.CLIP_READY,
            1_013,
            request="r1",
            shot="s1",
            duration=5.0,
            qa=QAVerdict.PASS,
        ),
        _render(RenderEventKind.RENDER_REQUESTED, 2_000, request="r2", shot="s2"),
        _render(
            RenderEventKind.CLIP_READY,
            2_020,
            request="r2",
            shot="s2",
            duration=5.0,
            qa=QAVerdict.PASS,
        ),
    ]
    result, outputs = build_render_qa_pipeline(events, window_ms=60_000)
    lats = [w.result for w in result.typed_values(outputs.latency_node, WindowResult)]
    assert lats
    pct: LatencyPercentiles = lats[0]
    assert pct.count == 2
    assert pct.max_ms is not None and pct.max_ms >= 12  # the slower of the two


def test_accepted_footage_efficiency() -> None:
    summaries = [
        QASummary(finished_shots=10, accepted_shots=9, regenerations=1, accepted_seconds=45.0)
    ]
    eff = accepted_footage_efficiency(summaries)
    assert eff is not None and abs(eff - 90.0) < 1e-9
    assert accepted_footage_efficiency([]) is None
