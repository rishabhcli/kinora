"""Render throughput & QA dashboards over the render-event stream (§5.6, §13).

The render-event stream carries every generation event: a shot was requested,
a keyframe / clip landed, the Critic passed or failed it, a regen completed,
the budget ran low. This pipeline turns it into the §13 metrics the demo chart
needs, in event time:

* **Throughput** — clips accepted per tumbling window (shots/min) and the
  running total of accepted video-seconds.

* **Accept rate & regeneration rate** — the headline §13 numbers. Accept rate is
  the windowed mean of an "accepted" 0/1 indicator over finished shots;
  regeneration rate is regenerations / total shots — the crew should beat the
  single-agent baseline because memory conditions each shot correctly the first
  time.

* **End-to-end render latency** — a *stream-stream interval join* of
  ``render_requested`` against its later ``clip_ready`` (matched on
  ``request_id``), yielding per-shot wall-clock latency; a tumbling window then
  reports p50 / p95 (§13 "latency-to-first-frame").

* **CCS by character/shot** — windowed mean character-consistency score (§13),
  the consistency half of the demo chart.

* **Budget burn-down** — the latest ``budget_remaining_s`` per book (a
  stream-table style last-write-wins), for the §11.1 ``budget_low`` guardrail.

Pure topology factories returning an
:class:`~app.streaming.processing.runtime.ExecutionResult`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.streaming.processing.aggregations import (
    MeanAggregate,
    SumAggregate,
    percentile,
)
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.pipelines.events import (
    RenderEvent,
    RenderEventKind,
)
from app.streaming.processing.runtime import ExecutionResult
from app.streaming.processing.time_domain import WatermarkStrategy, field_timestamp_assigner
from app.streaming.processing.windows import TumblingEventTimeWindows

DEFAULT_OUT_OF_ORDERNESS_MS = 2_000


def render_watermark_strategy() -> WatermarkStrategy[RenderEvent]:
    return WatermarkStrategy.for_bounded_out_of_orderness(
        field_timestamp_assigner(lambda e: e.ts_ms), DEFAULT_OUT_OF_ORDERNESS_MS
    )


# --------------------------------------------------------------------------- #
# Latency: the request -> clip interval join produces these.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RenderLatency:
    """A shot's end-to-end render latency (ms) from request to clip-ready."""

    request_id: str
    shot_id: str
    book_id: str
    latency_ms: int
    accepted: bool


def _join_latency(req: RenderEvent, clip: RenderEvent) -> RenderLatency:
    return RenderLatency(
        request_id=req.request_id or req.shot_id,
        shot_id=clip.shot_id or req.shot_id,
        book_id=clip.book_id,
        latency_ms=max(0, clip.ts_ms - req.ts_ms),
        accepted=clip.is_accepted_clip,
    )


# --------------------------------------------------------------------------- #
# Percentile aggregate over a latency window (buffers then summarizes).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LatencyPercentiles:
    """p50 / p95 / max render latency over a window (ms)."""

    count: int
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


class LatencyPercentileAggregate:
    """Collects latencies in a window then reports p50 / p95 / max."""

    def create_accumulator(self) -> list[float]:
        return []

    def add(self, value: RenderLatency, acc: list[float]) -> list[float]:
        acc.append(float(value.latency_ms))
        return acc

    def get_result(self, acc: list[float]) -> LatencyPercentiles:
        return LatencyPercentiles(
            count=len(acc),
            p50_ms=percentile(acc, 0.5),
            p95_ms=percentile(acc, 0.95),
            max_ms=max(acc) if acc else None,
        )

    def merge(self, a: list[float], b: list[float]) -> list[float]:
        return [*a, *b]


# --------------------------------------------------------------------------- #
# Accept-rate / regen-rate aggregate over finished shots.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class QASummary:
    """Per-window QA rollup: the §13 headline metrics for a book."""

    finished_shots: int
    accepted_shots: int
    regenerations: int
    accepted_seconds: float

    @property
    def accept_rate(self) -> float | None:
        if self.finished_shots == 0:
            return None
        return self.accepted_shots / self.finished_shots

    @property
    def regeneration_rate(self) -> float | None:
        if self.finished_shots == 0:
            return None
        return self.regenerations / self.finished_shots


@dataclass(frozen=True, slots=True)
class _QAAcc:
    finished: int = 0
    accepted: int = 0
    regens: int = 0
    accepted_seconds: float = 0.0


class QARollupAggregate:
    """Folds finished-shot events into a :class:`QASummary`.

    Only clip-bearing / regen events are fed in (the topology filters first), so
    a window's counts are over *finished shots*, matching the §13 denominators.
    """

    def create_accumulator(self) -> _QAAcc:
        return _QAAcc()

    def add(self, value: RenderEvent, acc: _QAAcc) -> _QAAcc:
        accepted = value.is_accepted_clip
        regen = value.is_regeneration
        finished = value.kind in (RenderEventKind.CLIP_READY, RenderEventKind.REGEN_DONE)
        return _QAAcc(
            finished=acc.finished + (1 if finished else 0),
            accepted=acc.accepted + (1 if accepted else 0),
            regens=acc.regens + (1 if regen else 0),
            accepted_seconds=acc.accepted_seconds + (value.duration_s if accepted else 0.0),
        )

    def get_result(self, acc: _QAAcc) -> QASummary:
        return QASummary(
            finished_shots=acc.finished,
            accepted_shots=acc.accepted,
            regenerations=acc.regens,
            accepted_seconds=acc.accepted_seconds,
        )

    def merge(self, a: _QAAcc, b: _QAAcc) -> _QAAcc:
        return _QAAcc(
            finished=a.finished + b.finished,
            accepted=a.accepted + b.accepted,
            regens=a.regens + b.regens,
            accepted_seconds=a.accepted_seconds + b.accepted_seconds,
        )


# --------------------------------------------------------------------------- #
# Topology factory
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RenderQAOutputs:
    """Node ids of the render-QA topology's outputs."""

    throughput_node: str
    qa_rollup_node: str
    ccs_node: str
    latency_node: str


def build_render_qa_pipeline(
    events: list[RenderEvent],
    *,
    window_ms: int = 60_000,
    latency_lower_ms: int = 0,
    latency_upper_ms: int = 120_000,
    allowed_lateness_ms: int = 5_000,
) -> tuple[ExecutionResult, RenderQAOutputs]:
    """Wire and run the render-QA topology over ``events``.

    The latency join window ``[0, 120s]`` reflects §4.1: a clip lands ~12s after
    its request in the happy path, well inside the bound; 120s is a generous
    upper edge that still evicts buffered requests so the join never leaks.
    """

    env = StreamEnvironment()
    src = env.from_source(events, name="render-events").assign_timestamps_and_watermarks(
        render_watermark_strategy()
    )

    # 1. throughput: accepted video-seconds per window, keyed by book.
    accepted = src.filter(lambda e: e.is_accepted_clip).key_by(lambda e: e.book_id)
    throughput = accepted.window(TumblingEventTimeWindows(size_ms=window_ms)).aggregate(
        SumAggregate(lambda e: e.duration_s), allowed_lateness_ms=allowed_lateness_ms
    )

    # 2. QA rollup: accept rate, regen rate, accepted seconds over finished shots.
    finished = src.filter(
        lambda e: e.kind in (RenderEventKind.CLIP_READY, RenderEventKind.REGEN_DONE)
    ).key_by(lambda e: e.book_id)
    qa_rollup = finished.window(TumblingEventTimeWindows(size_ms=window_ms)).aggregate(
        QARollupAggregate(),
        allowed_lateness_ms=allowed_lateness_ms,
    )

    # 3. CCS: windowed mean character-consistency score (§13).
    ccs = (
        src.filter(lambda e: e.ccs is not None)
        .key_by(lambda e: e.book_id)
        .window(TumblingEventTimeWindows(size_ms=window_ms))
        .aggregate(
            MeanAggregate(lambda e: float(e.ccs or 0.0)),
            allowed_lateness_ms=allowed_lateness_ms,
        )
    )

    # 4. end-to-end latency: interval-join request -> clip on request_id, then p50/p95.
    requests = src.filter(lambda e: e.kind == RenderEventKind.RENDER_REQUESTED).key_by(
        lambda e: e.request_id or e.shot_id
    )
    clips = src.filter(lambda e: e.kind == RenderEventKind.CLIP_READY).key_by(
        lambda e: e.request_id or e.shot_id
    )
    latencies = requests.interval_join(
        clips,
        lower_ms=latency_lower_ms,
        upper_ms=latency_upper_ms,
        join_fn=_join_latency,
    )
    latency = (
        latencies.key_by(lambda lat: lat.book_id)
        .window(TumblingEventTimeWindows(size_ms=window_ms))
        .aggregate(
            LatencyPercentileAggregate(),
            allowed_lateness_ms=allowed_lateness_ms,
        )
    )

    outputs = RenderQAOutputs(
        throughput_node=throughput.node_id,
        qa_rollup_node=qa_rollup.node_id,
        ccs_node=ccs.node_id,
        latency_node=latency.node_id,
    )
    return env.execute(name="render-qa"), outputs


def accepted_footage_efficiency(qa_summaries: list[QASummary]) -> float | None:
    """The §13 headline: ``(1 - rejected/total) * 100`` over finished shots.

    Computed across one or more QA-rollup windows. ``rejected`` is approximated
    by regenerations (a failed shot is rejected footage); ``total`` is finished
    shots. Returns ``None`` with no finished shots.
    """

    total = sum(s.finished_shots for s in qa_summaries)
    if total == 0:
        return None
    regens = sum(s.regenerations for s in qa_summaries)
    return (1.0 - regens / total) * 100.0
