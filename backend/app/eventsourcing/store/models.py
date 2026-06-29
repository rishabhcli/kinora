"""ORM models for the append-only event store (event-sourcing facet A).

Five additive tables, all ``es_``-prefixed and self-contained so they can ship
without touching any existing model file. They register on ``Base.metadata`` (via
the additive import in ``app/db/models/__init__.py``) so Alembic autogenerate and
``create_all`` see them.

* :class:`EventStoreEvent` (``es_events``) — the log. ``global_position`` (BIGINT)
  is the dense, gap-free store-wide order and the PK; unique ``(stream_id,
  version)`` is the per-stream order and the hard optimistic-concurrency backstop;
  unique ``event_id`` is the dedup/idempotency key.
* :class:`EventStoreSnapshot` (``es_snapshots``) — aggregate snapshots keyed by
  ``(stream_id, snapshot_type)`` keeping the newest version.
* :class:`EventStoreOutbox` (``es_outbox``) — the transactional OUTBOX (§12.1):
  one ``pending`` row per published event, written in the append txn.
* :class:`EventStoreInbox` (``es_inbox``) — the idempotent INBOX (§12.1):
  ``(consumer, message_id)`` PK records effectively-once consumption.
* :class:`EventStoreSequence` (``es_sequence``) — the single gap-free global
  counter row (Postgres SEQUENCEs burn numbers on rollback; this one does not).

JSONB columns carry the event payload and the metadata envelope
(correlation/causation/actor/headers). Enum-valued columns are plain VARCHAR
carrying the lowercase :class:`~app.eventsourcing.store.contracts.OutboxStatus`
values, matching the notifications-platform convention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, new_id

#: Name of the single global-sequence counter row in ``es_sequence``.
GLOBAL_SEQUENCE_NAME = "es_global_position"


class EventStoreEvent(Base):
    """One immutable event in the log (``es_events``)."""

    __tablename__ = "es_events"
    __table_args__ = (
        UniqueConstraint("stream_id", "version", name="uq_es_events_stream_id_version"),
        UniqueConstraint("event_id", name="uq_es_events_event_id"),
        # Catch-up subscriptions page by global_position; it is already the PK
        # (indexed). Stream rehydration pages by (stream_id, version) — covered by
        # the unique constraint's index. A correlation index supports tracing.
        Index("ix_es_events_correlation", "correlation_id"),
        Index("ix_es_events_event_type", "event_type"),
    )

    #: The dense, gap-free, store-wide order (assigned from es_sequence). PK.
    global_position: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    #: The dedup / idempotency key for the event occurrence.
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The stream this event belongs to (one aggregate instance).
    stream_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 0-based, dense position within the stream.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Logical, schema-versioned event-type name.
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The event payload (JSON object).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: The metadata envelope (correlation/causation/actor/headers).
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Denormalised correlation id (also inside metadata) for a cheap trace index.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Server-assigned append timestamp (UTC).
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventStoreSnapshot(Base):
    """A point-in-time aggregate snapshot (``es_snapshots``)."""

    __tablename__ = "es_snapshots"

    #: One newest snapshot per (stream, type). Composite PK.
    stream_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    snapshot_type: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    #: The stream version the state reflects.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The serialised aggregate state (JSON object).
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventStoreOutbox(Base):
    """A transactional-outbox row — intent to publish one event (``es_outbox``)."""

    __tablename__ = "es_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_es_outbox_event_id"),
        # The relay claims pending, due rows ordered by global_position.
        Index("ix_es_outbox_claim", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    #: The event this row publishes (unique → one outbox row per event per topic).
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Global position of the event (drives claim ordering).
    global_position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Publish target / channel name.
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The message body to publish (the recorded event, projected to JSON).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Lifecycle: ``pending`` | ``published`` | ``dead`` (OutboxStatus values).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: Publish attempts so far (drives backoff + DLQ).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Earliest time the relay may (re)claim this row — backoff gate.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EventStoreInbox(Base):
    """An idempotent-inbox row — ``consumer`` handled ``message_id`` (``es_inbox``)."""

    __tablename__ = "es_inbox"

    consumer: Mapped[str] = mapped_column(String(255), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Optional processing result / receipt.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class EventStoreSequence(Base):
    """The single gap-free global-position counter row (``es_sequence``)."""

    __tablename__ = "es_sequence"

    #: Counter name (PK). Always :data:`GLOBAL_SEQUENCE_NAME` for the global log.
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The last allocated value (next allocation is ``value + 1``).
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class EventStoreCheckpoint(Base):
    """A projection's durable position in the global log (``es_checkpoints``).

    One row per ``(subscription)`` — typically a projection / read-model name.
    ``position`` is the highest ``global_position`` the subscription has fully
    processed (it resumes at ``position + 1``). ``status`` lets a projection be
    paused/marked-failed for ops; ``last_error`` records the last failure for the
    §12.5 observability surface. The catch-up subscription advances this exactly
    once per processed event, so a restart never reprocesses committed work and
    never skips a gap (the global log is gap-free).
    """

    __tablename__ = "es_checkpoints"

    subscription: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: Last fully-processed global position (resume at position + 1). 0 = start.
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: ``active`` | ``paused`` | ``failed`` (CheckpointStatus values).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: Count of events processed (observability; monotone).
    events_processed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "GLOBAL_SEQUENCE_NAME",
    "EventStoreCheckpoint",
    "EventStoreEvent",
    "EventStoreInbox",
    "EventStoreOutbox",
    "EventStoreSequence",
    "EventStoreSnapshot",
]
