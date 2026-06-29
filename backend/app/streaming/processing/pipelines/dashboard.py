"""The §13 metrics snapshot — both pipelines folded into one dashboard view.

The demo's single persuasive slide (§13) needs *numbers*: accepted-footage
efficiency, regeneration rate, CCS, buffer/latency health, live reading velocity.
Those come from the two stream pipelines:

* :mod:`engagement` over the reader-intent stream → velocity, reading sessions,
  stalls (buffer-health proxy),
* :mod:`render_qa` over the render-event stream → throughput, accept/regen rate,
  CCS, p50/p95 latency.

This module runs both and reduces their windowed outputs to a single
:class:`MetricsSnapshot` — the shape the §5.3 metrics panel / the demo chart
renders. It also derives the §4.6 *velocity-adaptive lookahead*: at the measured
reading velocity, how many seconds of video should be committed ahead. That ties
the analytics back to the control-plane policy without touching the scheduler.

Pure: takes the two event lists, returns a value object. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.streaming.processing.pipelines.engagement import (
    ReadingStall,
    build_engagement_pipeline,
    latest_velocity_by_session,
)
from app.streaming.processing.pipelines.events import ReaderIntentEvent, RenderEvent
from app.streaming.processing.pipelines.render_qa import (
    QASummary,
    accepted_footage_efficiency,
    build_render_qa_pipeline,
)
from app.streaming.processing.window_operator import WindowResult

# §4.6 commit horizon: video-seconds the buffer aims to hold ahead of the reader.
COMMIT_HORIZON_S = 45.0
# §4.1 asymmetry: a reader consumes ~0.15–0.30 video-seconds per wall-clock second.
VIDEO_SECONDS_PER_WORD = 5.0 / 30.0  # ~5s of video per ~30 words read


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """The §13 dashboard numbers, reduced from both streams.

    ``accepted_footage_efficiency`` and ``regeneration_rate`` are the headline
    §13 figures; ``mean_ccs`` is the consistency half; ``p95_latency_ms`` is the
    seek/first-frame health; ``mean_velocity_wps`` and ``stalls`` summarize
    engagement / buffer health.
    """

    finished_shots: int
    accepted_shots: int
    accepted_seconds: float
    regenerations: int
    accepted_footage_efficiency: float | None
    regeneration_rate: float | None
    mean_ccs: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    mean_velocity_wps: float | None
    reading_sessions: int
    stalls: int

    def recommended_lookahead_s(self) -> float:
        """§4.6 velocity-adaptive lookahead in video-seconds.

        A faster reader (higher ``v``) drains the buffer faster, so the
        committed buffer must hold more video-seconds ahead. Scales the commit
        horizon by the reader's velocity relative to a nominal 3 wps, clamped to
        a sane band. Mirrors the scheduler's self-tuning (§4.6) for the panel.
        """

        if self.mean_velocity_wps is None or self.mean_velocity_wps <= 0:
            return COMMIT_HORIZON_S
        nominal_wps = 3.0
        scaled = COMMIT_HORIZON_S * (self.mean_velocity_wps / nominal_wps)
        return max(COMMIT_HORIZON_S, min(scaled, COMMIT_HORIZON_S * 3.0))


def _reduce_qa(window_results: list[WindowResult[QASummary]]) -> tuple[int, int, float, int]:
    finished = sum(w.result.finished_shots for w in window_results)
    accepted = sum(w.result.accepted_shots for w in window_results)
    seconds = sum(w.result.accepted_seconds for w in window_results)
    regens = sum(w.result.regenerations for w in window_results)
    return finished, accepted, seconds, regens


def _mean_of(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_metrics_snapshot(
    reader_events: list[ReaderIntentEvent],
    render_events: list[RenderEvent],
) -> MetricsSnapshot:
    """Run both pipelines over their event lists and fold to one snapshot."""

    eng_result, eng_out = build_engagement_pipeline(reader_events)
    qa_result, qa_out = build_render_qa_pipeline(render_events)

    # -- render-QA reductions ---------------------------------------------- #
    qa_windows = qa_result.typed_values(qa_out.qa_rollup_node, WindowResult)
    finished, accepted, seconds, regens = _reduce_qa(qa_windows)
    summaries = [w.result for w in qa_windows]
    efficiency = accepted_footage_efficiency(summaries)
    regen_rate = (regens / finished) if finished else None

    ccs_windows = qa_result.typed_values(qa_out.ccs_node, WindowResult)
    mean_ccs = _mean_of([w.result for w in ccs_windows if w.result is not None])

    latency_windows = qa_result.typed_values(qa_out.latency_node, WindowResult)
    p50s = [w.result.p50_ms for w in latency_windows if w.result.p50_ms is not None]
    p95s = [w.result.p95_ms for w in latency_windows if w.result.p95_ms is not None]

    # -- engagement reductions --------------------------------------------- #
    velocities = latest_velocity_by_session(eng_result, eng_out.velocity_node)
    mean_velocity = _mean_of(list(velocities.values()))
    sessions = len(eng_result.typed_values(eng_out.session_node, WindowResult))
    stalls = len(eng_result.typed_values(eng_out.stall_node, ReadingStall))

    return MetricsSnapshot(
        finished_shots=finished,
        accepted_shots=accepted,
        accepted_seconds=seconds,
        regenerations=regens,
        accepted_footage_efficiency=efficiency,
        regeneration_rate=regen_rate,
        mean_ccs=mean_ccs,
        p50_latency_ms=_mean_of(p50s),
        p95_latency_ms=max(p95s) if p95s else None,
        mean_velocity_wps=mean_velocity,
        reading_sessions=sessions,
        stalls=stalls,
    )


def crew_vs_baseline_delta(crew: MetricsSnapshot, baseline: MetricsSnapshot) -> dict[str, float]:
    """The §13 comparison: how the crew beats the single-agent baseline.

    Returns the deltas the demo chart plots — efficiency and CCS gains, the
    regeneration-rate reduction. Positive = crew wins. ``None`` metrics are
    treated as 0 so the delta is always a number for the chart.
    """

    def g(x: float | None) -> float:
        return x if x is not None else 0.0

    return {
        "efficiency_gain": g(crew.accepted_footage_efficiency)
        - g(baseline.accepted_footage_efficiency),
        "ccs_gain": g(crew.mean_ccs) - g(baseline.mean_ccs),
        "regen_rate_reduction": g(baseline.regeneration_rate) - g(crew.regeneration_rate),
    }
