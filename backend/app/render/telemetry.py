"""A typed per-state telemetry bus for the §9.7 render engine (§12.5).

The pipeline already emits structured *logs* and Prometheus counters at each
transition. This module adds a **typed event stream** on top, so the hardening
layers (checkpoint, retry, poison, DAG) and the simulator can all publish and
consume the *same* sequence of facts:

* a :class:`RenderEvent` is a small frozen record (state entered, rung selected,
  retry scheduled, checkpoint written, shot poisoned, step skipped, shot
  finished) — every event is JSON-serialisable for the §13 demo "what-if" panel;
* a :class:`TelemetryBus` fans events to any number of :class:`TelemetrySink`s;
* :class:`RecordingSink` keeps a bounded in-memory trace (the simulator reads it
  back); :class:`MetricsSink` translates events into the additive Prometheus
  series in :mod:`app.observability.metrics`; :class:`LogSink` mirrors them to
  structured logs.

The bus is deliberately synchronous and dependency-light: publishing an event is
a cheap, non-throwing call safe from any hot path (a sink that raises is isolated
so one bad sink can never break a render). Metrics emission is therefore *exactly*
the additive surface — no existing series are touched.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.logging import get_logger
from app.observability import metrics
from app.render.ladder import Rung
from app.render.states import RenderState

logger = get_logger("app.render.telemetry")


class EventKind(StrEnum):
    """The render-engine event taxonomy (§12.5)."""

    STATE_ENTERED = "state_entered"
    RUNG_SELECTED = "rung_selected"
    RETRY_SCHEDULED = "retry_scheduled"
    CHECKPOINTED = "checkpointed"
    RESUMED = "resumed"
    STEP_SKIPPED = "step_skipped"
    POISONED = "poisoned"
    SHOT_FINISHED = "shot_finished"


@dataclass(frozen=True, slots=True)
class RenderEvent:
    """One telemetry fact about a shot's progress through the §9.7 machine.

    ``seq`` is a monotone per-bus sequence number so a recorded trace has a total
    order even when timestamps collide; ``data`` carries kind-specific detail
    (kept JSON-friendly). Construct via the ``RenderEvent.*`` builders below.
    """

    shot_id: str
    kind: EventKind
    seq: int = 0
    state: RenderState | None = None
    rung: Rung | None = None
    attempt: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view (enums flattened to their values)."""
        out: dict[str, Any] = {"shot_id": self.shot_id, "kind": self.kind.value, "seq": self.seq}
        if self.state is not None:
            out["state"] = self.state.value
        if self.rung is not None:
            out["rung"] = self.rung.value
        if self.attempt is not None:
            out["attempt"] = self.attempt
        if self.data:
            out["data"] = dict(self.data)
        return out

    # -- builders (the only sanctioned way to mint an event) ---------------- #

    @staticmethod
    def state_entered(shot_id: str, state: RenderState) -> RenderEvent:
        return RenderEvent(shot_id=shot_id, kind=EventKind.STATE_ENTERED, state=state)

    @staticmethod
    def rung_selected(shot_id: str, rung: Rung, *, reason: str | None = None) -> RenderEvent:
        return RenderEvent(
            shot_id=shot_id,
            kind=EventKind.RUNG_SELECTED,
            rung=rung,
            data={"reason": reason} if reason else {},
        )

    @staticmethod
    def retry_scheduled(
        shot_id: str, attempt: int, *, action: str, backoff_s: float
    ) -> RenderEvent:
        return RenderEvent(
            shot_id=shot_id,
            kind=EventKind.RETRY_SCHEDULED,
            attempt=attempt,
            data={"action": action, "backoff_s": backoff_s},
        )

    @staticmethod
    def checkpointed(shot_id: str, state: RenderState, *, attempt: int) -> RenderEvent:
        return RenderEvent(
            shot_id=shot_id, kind=EventKind.CHECKPOINTED, state=state, attempt=attempt
        )

    @staticmethod
    def resumed(shot_id: str, state: RenderState, *, attempt: int) -> RenderEvent:
        return RenderEvent(shot_id=shot_id, kind=EventKind.RESUMED, state=state, attempt=attempt)

    @staticmethod
    def step_skipped(shot_id: str, step: str) -> RenderEvent:
        return RenderEvent(shot_id=shot_id, kind=EventKind.STEP_SKIPPED, data={"step": step})

    @staticmethod
    def poisoned(shot_id: str, *, failures: int, reason: str) -> RenderEvent:
        return RenderEvent(
            shot_id=shot_id,
            kind=EventKind.POISONED,
            data={"failures": failures, "reason": reason},
        )

    @staticmethod
    def shot_finished(
        shot_id: str, state: RenderState, *, rung: Rung | None, video_seconds: float, attempts: int
    ) -> RenderEvent:
        return RenderEvent(
            shot_id=shot_id,
            kind=EventKind.SHOT_FINISHED,
            state=state,
            rung=rung,
            attempt=attempts,
            data={"video_seconds": video_seconds},
        )


class TelemetrySink(Protocol):
    """A consumer of :class:`RenderEvent`s (publishing must never raise)."""

    def emit(self, event: RenderEvent) -> None: ...


class RecordingSink:
    """A bounded, thread-safe in-memory trace (the simulator + demo read it back)."""

    def __init__(self, *, capacity: int = 4096) -> None:
        self._events: deque[RenderEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, event: RenderEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(
        self, *, shot_id: str | None = None, kind: EventKind | None = None
    ) -> list[RenderEvent]:
        """A filtered snapshot of the trace (ordered by sequence)."""
        with self._lock:
            snapshot = list(self._events)
        return [
            e
            for e in snapshot
            if (shot_id is None or e.shot_id == shot_id) and (kind is None or e.kind is kind)
        ]

    def as_dicts(self, **filters: Any) -> list[dict[str, Any]]:
        """The filtered trace as JSON-friendly dicts (for the demo panel)."""
        return [e.as_dict() for e in self.events(**filters)]

    def count(self, kind: EventKind) -> int:
        """How many events of a kind have been recorded."""
        return len(self.events(kind=kind))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def __iter__(self) -> Iterator[RenderEvent]:
        with self._lock:
            return iter(list(self._events))


class MetricsSink:
    """Translates events into the **additive** §12.5 Prometheus series.

    Only the render-engine series added in :mod:`app.observability.metrics` are
    touched here (checkpoint/resume/step-skipped/poison) — the existing per-shot
    counters stay owned by the pipeline so we never double-count.
    """

    def emit(self, event: RenderEvent) -> None:
        if event.kind is EventKind.CHECKPOINTED:
            metrics.inc_render_checkpoint()
        elif event.kind is EventKind.RESUMED:
            metrics.inc_render_resume()
        elif event.kind is EventKind.STEP_SKIPPED:
            metrics.inc_render_step_skipped(str(event.data.get("step", "unknown")))
        elif event.kind is EventKind.POISONED:
            metrics.inc_render_poison()


class LogSink:
    """Mirrors every event to structured logs (one line per fact)."""

    def emit(self, event: RenderEvent) -> None:
        logger.info("render.event", **event.as_dict())


class TelemetryBus:
    """Fans :class:`RenderEvent`s to its sinks; assigns monotone sequence numbers.

    Thread-safe and crash-isolated: a sink that raises is logged and skipped so a
    single bad consumer never breaks a render. The bus is cheap enough to live on
    the hot path — the no-op default (no sinks) costs only the seq increment.
    """

    def __init__(self, sinks: Iterable[TelemetrySink] | None = None) -> None:
        self._sinks: list[TelemetrySink] = list(sinks or [])
        self._lock = threading.Lock()
        self._seq = 0

    def add_sink(self, sink: TelemetrySink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def publish(self, event: RenderEvent) -> RenderEvent:
        """Stamp ``event`` with the next sequence number and fan it out."""
        with self._lock:
            self._seq += 1
            stamped = RenderEvent(
                shot_id=event.shot_id,
                kind=event.kind,
                seq=self._seq,
                state=event.state,
                rung=event.rung,
                attempt=event.attempt,
                data=event.data,
            )
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.emit(stamped)
            except Exception as exc:  # noqa: BLE001 - one bad sink must not break a render
                logger.warning("telemetry.sink_error", error=str(exc), kind=stamped.kind.value)
        return stamped

    @property
    def seq(self) -> int:
        """The last assigned sequence number."""
        return self._seq


def recording_bus(
    *, with_metrics: bool = False, with_logs: bool = False
) -> tuple[TelemetryBus, RecordingSink]:
    """A convenience bus + its recorder (the common test/simulator wiring).

    ``with_metrics`` / ``with_logs`` attach the metrics + log sinks alongside the
    recorder when a caller wants live emission too.
    """
    recorder = RecordingSink()
    sinks: list[TelemetrySink] = [recorder]
    if with_metrics:
        sinks.append(MetricsSink())
    if with_logs:
        sinks.append(LogSink())
    return TelemetryBus(sinks), recorder


__all__ = [
    "EventKind",
    "LogSink",
    "MetricsSink",
    "RecordingSink",
    "RenderEvent",
    "TelemetryBus",
    "TelemetrySink",
    "recording_bus",
]
