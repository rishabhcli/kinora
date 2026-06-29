"""The event-log contract this facet *consumes* from sibling facet A.

Facet C (read-model projections) is the **read side** of the CQRS split: it
folds an append-only event log into queryable read models. It does **not** own
the event log — facet A (``backend/app/eventsourcing/`` core) owns appending and
the canonical storage. This module declares the *minimal* slice of facet A's
surface the projection runtime depends on, so:

* the read side compiles and its full test-suite runs against an in-memory fake
  **before** facet A's concrete ``EventStore`` lands in this worktree, and
* when facet A ships, its store satisfies :class:`EventStore` structurally
  (``Protocol``) with no import edge from facet A back into this package.

The contract is intentionally tiny and read-only from this side:

``StoredEvent``
    An immutable envelope: an opaque ``event_id`` (idempotency key), the
    ``stream_id`` it belongs to, a monotonically increasing per-stream
    ``stream_version`` (0-based), a globally monotonic ``global_position``
    (the ordering the projection runtime checkpoints against), the event
    ``type`` + JSON-able ``payload``, and ``recorded_at`` transaction time.

``EventStore`` (the consumed protocol)
    Read methods only: ``read_all`` for catch-up over the global stream from a
    checkpoint, ``read_stream`` for a single aggregate, ``head_position`` for
    lag math, and an async ``subscribe`` for the live tail. The runtime never
    *writes* through this protocol — the command side does that.

See ``DESIGN.md`` ("The consumed EventStore contract") for the full rationale
and the boundary rules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# A global position is a single monotonically-increasing integer over the whole
# log. 0 is the position *before* the first event; the first appended event has
# global_position == 1. A checkpoint of ``0`` therefore means "nothing consumed
# yet" and replays the entire log.
GlobalPosition = int

#: The conventional "before anything" checkpoint value.
NO_POSITION: GlobalPosition = 0


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An immutable event as persisted by facet A and read by the projectors.

    Equality/hash is by ``event_id`` alone (the idempotency key) so a re-delivered
    event compares equal to its first delivery regardless of metadata drift.
    """

    event_id: str
    stream_id: str
    stream_version: int
    global_position: GlobalPosition
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    recorded_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __eq__(self, other: object) -> bool:  # pragma: no cover - trivial
        if not isinstance(other, StoredEvent):
            return NotImplemented
        return self.event_id == other.event_id

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(self.event_id)


@runtime_checkable
class EventStore(Protocol):
    """The read-only slice of facet A's store that the read side depends on.

    Structural (``Protocol``) so facet A's concrete store satisfies it without a
    declared inheritance edge. All methods are async to match the rest of the
    backend's data layer.
    """

    async def read_all(
        self,
        *,
        after_position: GlobalPosition = NO_POSITION,
        limit: int | None = None,
        types: Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        """Return events with ``global_position > after_position``, in position order.

        ``limit`` caps the batch (the runtime pages with it for catch-up); ``types``
        optionally restricts to a set of event types (a server-side filter the
        runtime uses when a projection only cares about a few types).
        """
        ...

    async def read_stream(
        self,
        stream_id: str,
        *,
        after_version: int = -1,
        as_of: datetime | None = None,
    ) -> list[StoredEvent]:
        """Return one stream's events in ``stream_version`` order.

        ``after_version`` (exclusive, default ``-1`` ⇒ from the start) supports
        incremental aggregate loads; ``as_of`` (transaction time) supports
        temporal reads (events recorded at/before that instant) per §8.5.
        """
        ...

    async def head_position(self) -> GlobalPosition:
        """The current maximum ``global_position`` in the log (0 if empty)."""
        ...

    def subscribe(
        self,
        *,
        after_position: GlobalPosition = NO_POSITION,
        poll_interval_s: float = 0.25,
    ) -> AsyncIterator[StoredEvent]:
        """A live tail: yield events as they are appended, starting after a position.

        Implementations may poll (the in-memory fake does) or push (a LISTEN/NOTIFY
        store). The runtime treats this as an infinite async iterator it can cancel.
        """
        ...


__all__ = [
    "NO_POSITION",
    "EventStore",
    "GlobalPosition",
    "StoredEvent",
]
