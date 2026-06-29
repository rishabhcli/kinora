"""Live engagement & velocity analytics over the reader-intent stream (§4.3).

The reader-intent stream is the product's heartbeat: every debounced scroll
settle, every velocity sample, every seek and idle-pause. This pipeline turns
that raw signal into the dashboards the §13 metrics panel and the live director
bar want, all in event time so it is correct under out-of-order delivery:

* **Windowed reading velocity** — a *sliding* event-time window over each
  session's velocity samples gives a smooth ``mean velocity_wps`` that updates
  every few seconds. This is the same ``v`` the Scheduler self-tunes on (§4.6),
  surfaced for observability.

* **Reading-session shaping** — a *session* window (gap-based) collapses a burst
  of activity into one reading session and ends it after an idle gap — exactly
  Kinora's idle-pause concept (§4.7). Output: session duration, words read,
  furthest ``focus_word``.

* **Tumbling engagement counts** — events per fixed window, split by
  :class:`IntentKind`, for an activity sparkline.

* **Stall detection** — a keyed :class:`ProcessFunction` that arms an event-time
  timer on each forward sample and fires a ``ReadingStall`` if no further
  forward motion arrives within a deadline (the reader is stuck / the buffer
  drained). This is the streaming analogue of the §4.11 "reader reads faster
  than render → stall" guard, observed rather than prevented.

Everything is a pure topology factory returning an
:class:`~app.streaming.processing.runtime.ExecutionResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from app.streaming.processing.aggregations import CountAggregate, MeanAggregate
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.operators import Collector, ProcessFunction
from app.streaming.processing.pipelines.events import IntentKind, ReaderIntentEvent
from app.streaming.processing.records import StreamRecord
from app.streaming.processing.runtime import ExecutionResult
from app.streaming.processing.state import ValueStateDescriptor
from app.streaming.processing.time_domain import WatermarkStrategy, field_timestamp_assigner
from app.streaming.processing.window_operator import WindowResult
from app.streaming.processing.windows import (
    SessionWindows,
    SlidingEventTimeWindows,
    TumblingEventTimeWindows,
)

# Default out-of-orderness for the reader stream: client clocks + debounce jitter.
DEFAULT_OUT_OF_ORDERNESS_MS = 1_000


def reader_watermark_strategy() -> WatermarkStrategy[ReaderIntentEvent]:
    """Event-time strategy reading ``ts_ms`` with bounded out-of-orderness."""

    return WatermarkStrategy.for_bounded_out_of_orderness(
        field_timestamp_assigner(lambda e: e.ts_ms), DEFAULT_OUT_OF_ORDERNESS_MS
    )


# --------------------------------------------------------------------------- #
# Custom aggregate: a reading-session summary over a session window.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReadingSessionSummary:
    """The shape a session window emits: a coherent reading session (§4.7)."""

    samples: int
    min_word: int
    max_word: int
    mean_velocity_wps: float | None
    start_ms: int
    end_ms: int

    @property
    def words_advanced(self) -> int:
        return max(0, self.max_word - self.min_word)


@dataclass(frozen=True, slots=True)
class _SessionAcc:
    samples: int = 0
    min_word: int = 2**63 - 1
    max_word: int = 0
    vel_total: float = 0.0
    vel_count: int = 0
    start_ms: int = 2**63 - 1
    end_ms: int = 0


class ReadingSessionAggregate:
    """Folds reader-intent events into a :class:`ReadingSessionSummary`."""

    def create_accumulator(self) -> _SessionAcc:
        return _SessionAcc()

    def add(self, value: ReaderIntentEvent, acc: _SessionAcc) -> _SessionAcc:
        return _SessionAcc(
            samples=acc.samples + 1,
            min_word=min(acc.min_word, value.focus_word),
            max_word=max(acc.max_word, value.focus_word),
            vel_total=acc.vel_total + value.velocity_wps,
            vel_count=acc.vel_count + 1,
            start_ms=min(acc.start_ms, value.ts_ms),
            end_ms=max(acc.end_ms, value.ts_ms),
        )

    def get_result(self, acc: _SessionAcc) -> ReadingSessionSummary:
        mean = acc.vel_total / acc.vel_count if acc.vel_count else None
        return ReadingSessionSummary(
            samples=acc.samples,
            min_word=acc.min_word if acc.samples else 0,
            max_word=acc.max_word,
            mean_velocity_wps=mean,
            start_ms=acc.start_ms if acc.samples else 0,
            end_ms=acc.end_ms,
        )

    def merge(self, a: _SessionAcc, b: _SessionAcc) -> _SessionAcc:
        return _SessionAcc(
            samples=a.samples + b.samples,
            min_word=min(a.min_word, b.min_word),
            max_word=max(a.max_word, b.max_word),
            vel_total=a.vel_total + b.vel_total,
            vel_count=a.vel_count + b.vel_count,
            start_ms=min(a.start_ms, b.start_ms),
            end_ms=max(a.end_ms, b.end_ms),
        )


# --------------------------------------------------------------------------- #
# Stall detection — keyed process function with an event-time timer.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReadingStall:
    """Emitted when a session shows no forward motion for ``deadline_ms``."""

    session_id: str
    last_word: int
    last_seen_ms: int
    detected_at_ms: int


class StallDetector(ProcessFunction[ReaderIntentEvent, ReadingStall]):
    """Fires a :class:`ReadingStall` when forward motion lapses past a deadline.

    On each forward sample it (re)arms an event-time timer at
    ``ts + deadline_ms`` and remembers the last position. When the timer fires
    (the watermark crossed the deadline with no newer forward sample resetting
    it) a stall is emitted. A seek or idle event clears the pending timer — the
    reader deliberately stopping is not a stall.
    """

    _NS = "stall"

    def __init__(self, *, deadline_ms: int) -> None:
        self._deadline = deadline_ms
        self._last_word: ValueStateDescriptor[int] = ValueStateDescriptor("stall-word", default=0)
        self._last_seen: ValueStateDescriptor[int] = ValueStateDescriptor("stall-seen", default=0)
        self._armed: ValueStateDescriptor[int] = ValueStateDescriptor("stall-armed", default=-1)

    def process_element(
        self, record: StreamRecord[ReaderIntentEvent], out: Collector[ReadingStall]
    ) -> None:
        event = record.value
        armed = self.ctx.state.get_value_state(self._armed)
        prev = armed.value()
        if prev is not None and prev >= 0:
            self.ctx.timers.delete(record.key, self._NS, prev)

        if event.kind in (IntentKind.SEEK, IntentKind.IDLE) or not event.is_forward:
            armed.update(-1)  # deliberate stop, no stall timer
            return

        self.ctx.state.get_value_state(self._last_word).update(event.focus_word)
        self.ctx.state.get_value_state(self._last_seen).update(event.ts_ms)
        deadline = event.ts_ms + self._deadline
        self.ctx.timers.register(record.key, self._NS, deadline)
        armed.update(deadline)

    def on_timer(self, timestamp: int, namespace: str, out: Collector[ReadingStall]) -> None:
        if namespace != self._NS:
            return
        armed = self.ctx.state.get_value_state(self._armed)
        if armed.value() != timestamp:
            return  # superseded by a newer sample
        key = self.ctx.state.current_key
        out.collect_value(
            ReadingStall(
                session_id=str(key),
                last_word=self.ctx.state.get_value_state(self._last_word).value() or 0,
                last_seen_ms=self.ctx.state.get_value_state(self._last_seen).value() or 0,
                detected_at_ms=timestamp,
            ),
            timestamp=timestamp,
            key=key,
        )
        armed.update(-1)


# --------------------------------------------------------------------------- #
# Topology factories
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EngagementOutputs:
    """Node ids of the engagement topology's interesting outputs."""

    velocity_node: str
    session_node: str
    counts_node: str
    stall_node: str


def build_engagement_pipeline(
    events: list[ReaderIntentEvent],
    *,
    velocity_window_ms: int = 10_000,
    velocity_slide_ms: int = 5_000,
    session_gap_ms: int = 8_000,
    count_window_ms: int = 5_000,
    stall_deadline_ms: int = 15_000,
    allowed_lateness_ms: int = 2_000,
) -> tuple[ExecutionResult, EngagementOutputs]:
    """Wire and run the full engagement topology over ``events``.

    Defaults track kinora.md: the session gap is the §4.7 idle-pause (8s); the
    velocity window is the buffer-relevant horizon; the stall deadline is a
    generous "no forward motion" timeout. Returns the run result plus the node
    ids so a caller can pull each metric stream.
    """

    env = StreamEnvironment()
    source = env.from_source(events, name="reader-intent").assign_timestamps_and_watermarks(
        reader_watermark_strategy()
    )
    keyed = source.key_by(lambda e: e.session_id)

    # 1. sliding mean reading velocity (forward samples only)
    velocity = (
        keyed.filter(lambda e: e.is_forward)
        .window(SlidingEventTimeWindows(size_ms=velocity_window_ms, slide_ms=velocity_slide_ms))
        .aggregate(
            MeanAggregate(lambda e: e.velocity_wps), allowed_lateness_ms=allowed_lateness_ms
        )
    )

    # 2. reading-session shaping (gap-based session window)
    sessions = keyed.window(SessionWindows(gap_ms=session_gap_ms)).aggregate(
        ReadingSessionAggregate(),
        allowed_lateness_ms=allowed_lateness_ms,
    )

    # 3. tumbling activity counts
    counts = keyed.window(TumblingEventTimeWindows(size_ms=count_window_ms)).aggregate(
        CountAggregate(), allowed_lateness_ms=allowed_lateness_ms
    )

    # 4. stall detection
    stalls = keyed.process(lambda: StallDetector(deadline_ms=stall_deadline_ms))

    outputs = EngagementOutputs(
        velocity_node=velocity.node_id,
        session_node=sessions.node_id,
        counts_node=counts.node_id,
        stall_node=stalls.node_id,
    )
    return env.execute(name="engagement"), outputs


def latest_velocity_by_session(
    result: ExecutionResult, velocity_node: str
) -> dict[str, float]:
    """Reduce the velocity window stream to the latest mean per session.

    A small reader-side helper for the dashboard: the windowed stream emits many
    overlapping windows; the panel usually wants the most recent value per
    session.
    """

    latest: dict[str, tuple[int, float]] = {}
    for record in result.records(velocity_node):
        win_result = cast("WindowResult[float | None]", record.value)
        if win_result.result is None:
            continue
        key = str(win_result.key)
        end = win_result.window.end
        if key not in latest or end >= latest[key][0]:
            latest[key] = (end, win_result.result)
    return {k: v for k, (_, v) in latest.items()}
