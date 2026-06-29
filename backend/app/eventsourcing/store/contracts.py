"""The seam: immutable value objects + the protocols the facets consume.

This module is **pure** (no I/O, no SQLAlchemy) and is the single contract the
*domain* facet (aggregates) and the *projection* facet (read models) import. The
store has two implementations behind these protocols — :class:`InMemoryEventStore`
for tests and :class:`PostgresEventStore` for production — and a consumer should
never need to know which one it holds.

Vocabulary
----------
* **stream** — an ordered sequence of events sharing a ``stream_id`` (typically
  one aggregate instance, e.g. ``canon-char_elsa_001`` or ``session-sess_7af3``).
* **version** — the dense, 0-based position of an event *within its stream*.
* **global position** — the dense, gap-free, store-wide position of an event in
  the total order. Projections page by this.
* **event id** — a unique id for the event *occurrence*; the idempotency / dedup
  key. Distinct from the (stream, version) coordinate.

Time is always timezone-aware UTC.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.eventsourcing.store.versioning import ExpectedVersion

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    """A fresh unique event-occurrence id (32-char hex UUID4)."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Metadata envelope
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """The metadata envelope carried alongside every event payload.

    ``correlation_id`` ties together every event produced while handling one
    original trigger (e.g. one reader seek → many shot events); ``causation_id``
    points at the *immediate* event/command that caused this one, so a causal
    chain can be reconstructed. ``actor`` records who/what produced the event
    (an agent name, a user id, ``"scheduler"``). ``headers`` is an open bag for
    transport/tracing extensions.
    """

    correlation_id: str | None = None
    causation_id: str | None = None
    actor: str | None = None
    headers: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.headers)
        if self.correlation_id is not None:
            out["correlation_id"] = self.correlation_id
        if self.causation_id is not None:
            out["causation_id"] = self.causation_id
        if self.actor is not None:
            out["actor"] = self.actor
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EventMetadata:
        data = dict(data or {})
        correlation_id = data.pop("correlation_id", None)
        causation_id = data.pop("causation_id", None)
        actor = data.pop("actor", None)
        return cls(
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor=actor,
            headers=data,
        )

    def caused_by(self, parent: RecordedEvent) -> EventMetadata:
        """Return a child metadata whose causation chains from ``parent``.

        Inherits the parent's ``correlation_id`` (same logical transaction) and
        sets ``causation_id`` to the parent's ``event_id`` — the standard way to
        propagate causality when one event handler emits follow-up events.
        """
        return replace(
            self,
            correlation_id=self.correlation_id or parent.metadata.correlation_id or parent.event_id,
            causation_id=parent.event_id,
        )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EventData:
    """An event to be appended — the input side, before the store assigns order.

    ``event_id`` is the dedup/idempotency key; it defaults to a fresh UUID but a
    caller may supply a deterministic one to make an append idempotent across
    retries. ``event_type`` is the logical, schema-versioned name (e.g.
    ``"canon.entity.upserted.v1"``).
    """

    event_type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=new_event_id)
    metadata: EventMetadata = field(default_factory=EventMetadata)

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(self.payload, dict):  # defensive: payloads are JSON objects
            raise ValueError("payload must be a dict")


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """An event *as stored* — the output side, with order assigned.

    Carries both orderings: ``version`` (within the stream) and
    ``global_position`` (store-wide). ``recorded_at`` is the server-assigned
    append timestamp.
    """

    stream_id: str
    event_id: str
    event_type: str
    version: int
    global_position: int
    payload: dict[str, Any]
    metadata: EventMetadata
    recorded_at: datetime

    @property
    def correlation_id(self) -> str | None:
        return self.metadata.correlation_id

    @property
    def causation_id(self) -> str | None:
        return self.metadata.causation_id


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StreamSlice:
    """The result of reading (part of) a stream.

    ``is_end`` is True when this slice reached the current tail of the stream;
    a paging reader stops when it sees ``is_end``. ``last_version`` is the
    version of the last returned event (or :data:`NO_EVENTS` when empty).
    """

    stream_id: str
    events: tuple[RecordedEvent, ...]
    last_version: int
    is_end: bool

    @property
    def is_empty(self) -> bool:
        return len(self.events) == 0


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A point-in-time materialisation of an aggregate's state.

    ``version`` is the stream version the state reflects; rehydration replays
    only events with ``version > snapshot.version``. ``state`` is a JSON-able
    dict the aggregate knows how to deserialise.
    """

    stream_id: str
    version: int
    state: dict[str, Any]
    snapshot_type: str = "default"
    created_at: datetime = field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Checkpoints (catch-up subscriptions)
# --------------------------------------------------------------------------- #


class CheckpointStatus(enum.Enum):
    """Operational state of a projection's catch-up subscription."""

    #: Actively consuming the log.
    ACTIVE = "active"
    #: Administratively paused (ops); the subscription will not advance.
    PAUSED = "paused"
    #: A handler raised; the subscription stopped at ``position`` for inspection.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A projection's durable position in the global log.

    ``position`` is the highest ``global_position`` fully processed; the
    subscription resumes at ``position + 1``. A fresh subscription starts at
    ``position == 0`` (read from the very beginning, since positions are 1-based).
    """

    subscription: str
    position: int = 0
    status: CheckpointStatus = CheckpointStatus.ACTIVE
    events_processed: int = 0
    last_error: str | None = None


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #


class OutboxStatus(enum.Enum):
    """Lifecycle of a transactional-outbox row (§12.1)."""

    PENDING = "pending"
    PUBLISHED = "published"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """One pending publish — written in the same txn as its event (§12.1).

    ``available_at`` gates retry backoff: the relay only claims rows whose
    ``available_at`` has passed. ``attempts`` counts publish tries; on reaching
    the max it transitions to :attr:`OutboxStatus.DEAD` (the DLQ).
    """

    id: str
    event_id: str
    global_position: int
    topic: str
    payload: dict[str, Any]
    status: OutboxStatus
    attempts: int
    available_at: datetime
    created_at: datetime
    published_at: datetime | None = None
    last_error: str | None = None


# --------------------------------------------------------------------------- #
# Protocols — the seam the facets consume
# --------------------------------------------------------------------------- #


@runtime_checkable
class EventStore(Protocol):
    """The append-only event store contract.

    Implementations must guarantee, for a single store:

    * **Per-stream order** — events in a stream have dense, 0-based versions.
    * **Global order** — every event has a dense, gap-free ``global_position``
      that is consistent with the order of appends.
    * **Optimistic concurrency** — :meth:`append` honours ``expected_version``
      and raises :class:`OptimisticConcurrencyError` on a mismatch.
    * **Atomic batch** — a multi-event :meth:`append` is all-or-nothing.
    """

    async def append(
        self,
        stream_id: str,
        events: Sequence[EventData],
        *,
        expected_version: ExpectedVersion,
        publish_topic: str | None = None,
    ) -> tuple[RecordedEvent, ...]:
        """Append ``events`` to ``stream_id`` under an ``expected_version`` guard.

        When ``publish_topic`` is given, a transactional-outbox row is written
        for each appended event in the *same* transaction (reliable publish).
        Returns the events as recorded (with versions + global positions).
        """
        ...

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 0,
        limit: int | None = None,
    ) -> StreamSlice:
        """Read ``stream_id`` forward from ``from_version`` (inclusive)."""
        ...

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 100,
    ) -> tuple[RecordedEvent, ...]:
        """Read the global log forward from ``from_position`` (exclusive).

        Returns up to ``limit`` events with ``global_position > from_position``,
        ordered by global position — the catch-up subscription primitive.
        """
        ...

    async def stream_version(self, stream_id: str) -> int:
        """Current version of ``stream_id`` (:data:`NO_EVENTS` if absent)."""
        ...

    async def last_position(self) -> int:
        """The highest assigned global position (0 if the store is empty)."""
        ...


@runtime_checkable
class SnapshotStore(Protocol):
    """Storage for aggregate snapshots (fast rehydration)."""

    async def save(self, snapshot: Snapshot) -> None:
        """Persist (or replace) the snapshot for its (stream, version)."""
        ...

    async def load_latest(
        self, stream_id: str, *, snapshot_type: str = "default"
    ) -> Snapshot | None:
        """The newest snapshot for ``stream_id`` (or ``None``)."""
        ...


@runtime_checkable
class OutboxRepository(Protocol):
    """Read/claim side of the transactional outbox (used by the relay)."""

    async def claim_batch(self, *, limit: int, now: datetime | None = None) -> list[OutboxRecord]:
        """Atomically claim up to ``limit`` due, pending rows for publishing."""
        ...

    async def mark_published(self, ids: Sequence[str], *, now: datetime | None = None) -> None:
        """Mark the given outbox rows published."""
        ...

    async def mark_failed(
        self,
        record_id: str,
        *,
        error: str,
        retry_at: datetime,
        dead: bool,
    ) -> None:
        """Record a failed publish: bump attempts, set backoff or dead-letter."""
        ...


@runtime_checkable
class InboxRepository(Protocol):
    """Idempotent INBOX — effectively-once consumption (§12.1)."""

    async def already_processed(self, consumer: str, message_id: str) -> bool:
        """Whether ``consumer`` has already handled ``message_id``."""
        ...

    async def mark_processed(
        self,
        consumer: str,
        message_id: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Record processing; returns ``False`` if it was already recorded."""
        ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable position tracking for catch-up subscriptions (the read side)."""

    async def load(self, subscription: str) -> Checkpoint:
        """Return the subscription's checkpoint (a fresh one at position 0)."""
        ...

    async def save(self, checkpoint: Checkpoint) -> None:
        """Persist the subscription's position + status."""
        ...


@runtime_checkable
class MessagePublisher(Protocol):
    """The transport the :class:`OutboxRelay` publishes to.

    A real implementation pushes to Redis / a broker / a webhook; tests use a
    recording fake. ``publish`` must raise to signal a transient failure (the
    relay then backs off / dead-letters); returning normally means delivered.
    """

    async def publish(self, record: OutboxRecord) -> None:
        """Publish one outbox record; raise on a transient delivery failure."""
        ...


@runtime_checkable
class EventSerializer(Protocol):
    """Encode/decode an event payload to/from a storable JSON object."""

    def serialize(self, event: EventData) -> dict[str, Any]:
        """Return the JSON-able payload object for storage."""
        ...

    def deserialize(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the payload dict for a stored event (validated if registered)."""
        ...


__all__ = [
    "Checkpoint",
    "CheckpointStatus",
    "CheckpointStore",
    "EventData",
    "EventMetadata",
    "EventSerializer",
    "EventStore",
    "InboxRepository",
    "MessagePublisher",
    "OutboxRecord",
    "OutboxRepository",
    "OutboxStatus",
    "RecordedEvent",
    "Snapshot",
    "SnapshotStore",
    "StreamSlice",
    "new_event_id",
]
