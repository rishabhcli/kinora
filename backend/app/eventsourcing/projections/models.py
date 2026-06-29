"""ORM models for the read side's durable stores (Alembic ``esproj_0001``).

Three additive tables, all prefixed ``esproj_`` so they namespace cleanly and
touch no existing schema:

* :class:`ReadModelRecord` (``esproj_read_models``) — the materialised view rows:
  ``(namespace, key)`` unique, a JSONB ``value``, and an optimistic ``version``.
  This is the Postgres backing of :class:`ReadModelStore`.
* :class:`ProjectionCheckpointRecord` (``esproj_checkpoints``) — one row per
  projection (or blue/green slot), carrying the durable ``position``, health
  fields, and the active blue/green slot pointer. Backs :class:`CheckpointStore`
  and :class:`SlotDirectory`.
* :class:`AppliedEventRecord` (``esproj_applied_events``) — the at-least-once
  idempotency ledger: ``(projection, event_id)`` unique. A row's presence means
  "this projection already folded this event"; the runtime consults it before
  invoking a handler. Pruned below the committed position by the store.

None of these are foreign-keyed to the event log or to ``books``/``users``: read
models are derived, rebuildable state that must survive source deletions and be
truncatable independently (the conftest TRUNCATE-all isolation relies on that).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin


class ReadModelRecord(StrIdMixin, TimestampMixin, Base):
    """One materialised read-model row: ``(namespace, key) -> value`` + version."""

    __tablename__ = "esproj_read_models"
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_esproj_read_models_ns_key"),
    )

    namespace: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ProjectionCheckpointRecord(StrIdMixin, TimestampMixin, Base):
    """Durable position + health + blue/green slot pointer for one projection."""

    __tablename__ = "esproj_checkpoints"
    __table_args__ = (
        UniqueConstraint("projection", name="uq_esproj_checkpoints_projection"),
    )

    #: The projection identity (or slot-scoped name ``<name>::<colour>``).
    projection: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Highest fully-applied global position.
    position: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Last observed store head (for lag math).
    observed_head: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="catching_up", nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projection_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The active blue/green slot for the *canonical* projection name (NULL for
    #: slot-scoped checkpoint rows, which do not themselves carry a pointer).
    active_slot: Mapped[str | None] = mapped_column(String(8), nullable=True)


class AppliedEventRecord(StrIdMixin, TimestampMixin, Base):
    """Idempotency ledger: ``(projection, event_id)`` was folded (at-least-once)."""

    __tablename__ = "esproj_applied_events"
    __table_args__ = (
        UniqueConstraint("projection", "event_id", name="uq_esproj_applied_proj_event"),
    )

    projection: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    #: The position of the applied event (lets the store prune below the checkpoint).
    position: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


__all__ = [
    "AppliedEventRecord",
    "ProjectionCheckpointRecord",
    "ReadModelRecord",
]
