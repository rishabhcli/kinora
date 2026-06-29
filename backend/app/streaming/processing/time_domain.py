"""Event-time machinery: timestamp assigners, watermark strategies, timers.

Event-time processing decouples *when an event happened* from *when the system
saw it*. Three pieces make that work:

* **Timestamp assigner** — extracts the event time (epoch ms) from a record's
  payload. Kinora's reader-intent and render events both carry an explicit
  client timestamp, so the assigner is a field read.
* **Watermark strategy** — decides, given the timestamps seen so far, the
  current event-time watermark ``W``: the assertion that no record with event
  time ``<= W`` will arrive later. :class:`BoundedOutOfOrdernessWatermarks`
  tolerates a fixed amount of out-of-orderness; :class:`MonotonousWatermarks`
  assumes perfectly ordered input.
* **Timer service** — per-key event-time timers that fire when the watermark
  passes their deadline. Windows register a cleanup timer at *window-end +
  allowed-lateness*; process functions register their own.

All times are integer epoch milliseconds for determinism.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

from app.streaming.processing.records import MIN_WATERMARK, StreamRecord

T = TypeVar("T")
T_contra = TypeVar("T_contra", contravariant=True)


# --------------------------------------------------------------------------- #
# Timestamp assignment
# --------------------------------------------------------------------------- #
class TimestampAssigner(Protocol[T_contra]):
    """Extracts the event-time (epoch ms) of a record's value."""

    def __call__(self, value: T_contra, record_timestamp: int) -> int: ...


# --------------------------------------------------------------------------- #
# Watermark generation
# --------------------------------------------------------------------------- #
class WatermarkGenerator(Protocol):
    """Tracks event-time progress and reports the current watermark.

    ``on_event`` is called for every record's timestamp; ``current_watermark``
    returns the watermark to emit (monotonic non-decreasing — the caller drops
    regressions).
    """

    def on_event(self, event_timestamp: int) -> None: ...

    def current_watermark(self) -> int: ...


class BoundedOutOfOrdernessGenerator:
    """Emits ``max_seen_timestamp - bound`` as the watermark.

    Tolerates records arriving up to ``out_of_orderness_ms`` late relative to
    the highest timestamp seen. A record whose event time is below the current
    watermark is *late* (the window operator decides whether it is still within
    allowed-lateness or dropped).
    """

    def __init__(self, out_of_orderness_ms: int) -> None:
        if out_of_orderness_ms < 0:
            raise ValueError("out_of_orderness_ms must be >= 0")
        self._bound = out_of_orderness_ms
        self._max_ts = MIN_WATERMARK + out_of_orderness_ms + 1

    def on_event(self, event_timestamp: int) -> None:
        if event_timestamp > self._max_ts:
            self._max_ts = event_timestamp

    def current_watermark(self) -> int:
        return self._max_ts - self._bound - 1


class MonotonousGenerator:
    """Assumes ascending timestamps: the watermark is the last seen timestamp."""

    def __init__(self) -> None:
        self._max_ts = MIN_WATERMARK

    def on_event(self, event_timestamp: int) -> None:
        if event_timestamp > self._max_ts:
            self._max_ts = event_timestamp

    def current_watermark(self) -> int:
        return self._max_ts


@dataclass(slots=True)
class WatermarkStrategy(Generic[T]):
    """Binds a timestamp assigner to a watermark generator factory.

    Attach to a source via :meth:`DataStream.assign_timestamps_and_watermarks`.
    The runtime calls the assigner for each record, feeds the timestamp to the
    generator, and emits a watermark whenever it advances.
    """

    assigner: TimestampAssigner[T]
    generator_factory: Callable[[], WatermarkGenerator]

    @classmethod
    def for_bounded_out_of_orderness(
        cls, assigner: TimestampAssigner[T], out_of_orderness_ms: int
    ) -> WatermarkStrategy[T]:
        return cls(
            assigner=assigner,
            generator_factory=lambda: BoundedOutOfOrdernessGenerator(out_of_orderness_ms),
        )

    @classmethod
    def for_monotonous_timestamps(cls, assigner: TimestampAssigner[T]) -> WatermarkStrategy[T]:
        return cls(assigner=assigner, generator_factory=MonotonousGenerator)

    def assign(self, record: StreamRecord[T]) -> int:
        return self.assigner(record.value, record.timestamp)


class _FieldTimestampAssigner(Generic[T]):
    """A :class:`TimestampAssigner` that pulls epoch-ms from a value field."""

    def __init__(self, extractor: Callable[[T], int]) -> None:
        self._extractor = extractor

    def __call__(self, value: T, record_timestamp: int) -> int:
        return self._extractor(value)


def field_timestamp_assigner(extractor: Callable[[T], int]) -> TimestampAssigner[T]:
    """Build an assigner from a function that pulls epoch-ms out of a value."""

    return _FieldTimestampAssigner(extractor)


# --------------------------------------------------------------------------- #
# Timer service
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, order=True)
class _Timer:
    """A heap entry: ``(timestamp, key, namespace)`` ordered by fire time."""

    timestamp: int
    # ``key`` / ``namespace`` are not order keys at runtime (timestamp wins), but
    # dataclass(order=True) needs them comparable; we sort by timestamp first.
    sort_index: int = field(compare=True, default=0)


class EventTimeTimerService:
    """A per-operator min-heap of (event-time, key, namespace) timers.

    ``register`` schedules a timer; ``advance_watermark`` pops and returns every
    timer whose deadline ``<= watermark`` in ascending order, so the caller can
    fire windows / process callbacks deterministically. Timers are de-duplicated
    per ``(key, namespace, timestamp)`` so re-registering the same deadline is
    idempotent (matches Flink semantics).
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, object, str]] = []
        self._registered: set[tuple[object, str, int]] = set()
        self._seq = 0
        self._current_watermark = MIN_WATERMARK

    @property
    def current_watermark(self) -> int:
        return self._current_watermark

    def register(self, key: object, namespace: str, timestamp: int) -> None:
        sig = (key, namespace, timestamp)
        if sig in self._registered:
            return
        self._registered.add(sig)
        # tie-break by insertion sequence to keep firing order deterministic.
        heapq.heappush(self._heap, (timestamp, self._seq, key, namespace))
        self._seq += 1

    def delete(self, key: object, namespace: str, timestamp: int) -> None:
        self._registered.discard((key, namespace, timestamp))

    def advance_watermark(self, watermark: int) -> list[tuple[object, str, int]]:
        if watermark < self._current_watermark:
            return []
        self._current_watermark = watermark
        fired: list[tuple[object, str, int]] = []
        while self._heap and self._heap[0][0] <= watermark:
            timestamp, _seq, key, namespace = heapq.heappop(self._heap)
            sig = (key, namespace, timestamp)
            if sig not in self._registered:
                continue  # was deleted after scheduling
            self._registered.discard(sig)
            fired.append((key, namespace, timestamp))
        return fired

    def pending(self) -> int:
        return len(self._registered)


# Public alias matching the package __init__ export.
TimerService = EventTimeTimerService
