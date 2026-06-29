"""Stream elements: data records and watermarks.

A *stream* is a sequence of :class:`StreamElement`. There are exactly two kinds,
mirroring Flink's stream-element model:

* :class:`StreamRecord` — a data item carrying a value, an **event-time**
  timestamp (milliseconds since the Unix epoch), and an optional key. The key is
  assigned by a ``key_by`` operator and addresses keyed state and per-key timers.
* :class:`Watermark` — a control marker asserting that no record with an
  event-time ``<= timestamp`` will arrive after it. Watermarks drive event-time
  windows and timers and are the mechanism by which an out-of-order stream makes
  *event-time* progress independent of processing-time.

Timestamps are integer milliseconds throughout, which keeps the engine fully
deterministic (no float drift) and matches the wire timestamps Kinora already
uses (``last_activity_ms``, ``occurred_at`` → epoch-ms).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar

T = TypeVar("T")
K = TypeVar("K")

#: Sentinel timestamp meaning "no event-time assigned yet". Records ingested
#: before a timestamp assigner runs carry this; an assigner must stamp them
#: before any event-time operator sees them.
NO_TIMESTAMP: int = -(2**63)

#: The maximum watermark — emitted at end-of-stream to flush every pending
#: event-time window and timer. Equivalent to Flink's ``Watermark.MAX_WATERMARK``.
MAX_WATERMARK: int = 2**63 - 1

#: The minimum watermark — the initial event-time of every operator before any
#: real watermark arrives.
MIN_WATERMARK: int = -(2**63)


@dataclass(slots=True)
class StreamRecord(Generic[T]):
    """A single data element flowing through the DAG.

    ``timestamp`` is the **event time** in epoch milliseconds. ``key`` is set by
    a ``key_by`` operator and is ``None`` on a non-keyed stream.
    """

    value: T
    timestamp: int = NO_TIMESTAMP
    key: object | None = None

    @property
    def has_timestamp(self) -> bool:
        """True once an event-time timestamp has been assigned."""

        return self.timestamp != NO_TIMESTAMP

    def with_value(self, value: object) -> StreamRecord[object]:
        """Return a copy carrying ``value`` but the same timestamp and key."""

        return StreamRecord(value=value, timestamp=self.timestamp, key=self.key)

    def with_key(self, key: object | None) -> StreamRecord[T]:
        """Return a copy carrying ``key`` but the same value and timestamp."""

        return replace(self, key=key)

    def with_timestamp(self, timestamp: int) -> StreamRecord[T]:
        """Return a copy stamped with ``timestamp`` (epoch ms)."""

        return replace(self, timestamp=timestamp)


@dataclass(slots=True, frozen=True, order=True)
class Watermark:
    """An event-time progress marker.

    A watermark with timestamp ``t`` asserts: *all records with event time
    ``<= t`` have already arrived.* It is the trigger for event-time windows and
    timers. Watermarks are monotonically non-decreasing within a stream; the
    runtime drops any watermark that would regress.
    """

    timestamp: int

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, int):  # pragma: no cover - defensive
            raise TypeError("Watermark timestamp must be an int (epoch ms)")


#: A stream element is either a data record or a watermark.
StreamElement = StreamRecord[T] | Watermark


@dataclass(slots=True)
class LatePolicy:
    """How an event-time operator treats records that arrive after a window's
    watermark deadline (window-end + allowed-lateness).

    * ``allowed_lateness_ms`` extends a window's lifetime past its end so a
      slightly-late record still updates it (and re-fires the window).
    * Records later than that go to a **side output** (see
      :data:`LATE_RECORDS_TAG`) rather than being silently dropped, so a
      pipeline can count / inspect data loss.
    """

    allowed_lateness_ms: int = 0
    emit_late_to_side_output: bool = True


#: The side-output tag every window operator uses for records dropped as too
#: late. Downstream code reads it via :meth:`ExecutionResult.side_output`.
LATE_RECORDS_TAG: str = "late-records"


@dataclass(slots=True)
class SideOutput(Generic[T]):
    """A named secondary output channel of an operator (Flink's ``OutputTag``).

    The primary output carries the operator's main result; side outputs carry
    auxiliary streams such as dropped-late records or split branches.
    """

    tag: str
    records: list[StreamRecord[T]] = field(default_factory=list)
