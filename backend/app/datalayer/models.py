"""ORM models for the datalayer read side's durable stores (Alembic ``readmodel_proj_0001``).

Three additive tables, all prefixed ``datalayer_`` so they namespace cleanly and
touch no existing schema:

* :class:`DataLayerReadModelRecord` (``datalayer_read_models``) — the materialised
  view rows: ``(namespace, key)`` unique, a JSONB ``value``, and an optimistic
  ``version``. The Postgres backing of
  :class:`~app.datalayer.readmodel.ReadModelStore`.
* :class:`DataLayerCheckpointRecord` (``datalayer_checkpoints``) — one row per
  projection: the durable ``position``, the events-applied counter, and the
  health fields. Backs :class:`~app.datalayer.checkpoints.CheckpointStore`.
* :class:`DataLayerAppliedEventRecord` (``datalayer_applied_events``) — the
  at-least-once idempotency ledger: ``(projection, event_id)`` unique. A row's
  presence means "this projection already folded this event"; the runner consults
  it before invoking a handler. Pruned below the committed position.

None are foreign-keyed to the event log or to ``books`` / ``users``: read models
are derived, rebuildable state that must survive source deletions and be
truncatable independently. These definitions are deliberately separate from the
older ``esproj_*`` tables — that read side consumes a different (adapter) event
contract; this one reads ``RecordedEvent`` straight from the real store.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin


class DataLayerReadModelRecord(StrIdMixin, TimestampMixin, Base):
    """One materialised read-model row: ``(namespace, key) -> value`` + version."""

    __tablename__ = "datalayer_read_models"
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_datalayer_read_models_ns_key"),
    )

    namespace: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DataLayerCheckpointRecord(StrIdMixin, TimestampMixin, Base):
    """Durable position + health record for one projection."""

    __tablename__ = "datalayer_checkpoints"
    __table_args__ = (
        UniqueConstraint("projection", name="uq_datalayer_checkpoints_projection"),
    )

    projection: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Highest fully-applied global position (resume at ``position + 1``).
    position: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Last observed store head (for lag = observed_head - position).
    observed_head: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    events_applied: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="catching_up", nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projection_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DataLayerAppliedEventRecord(StrIdMixin, TimestampMixin, Base):
    """Idempotency ledger: ``(projection, event_id)`` was folded (at-least-once)."""

    __tablename__ = "datalayer_applied_events"
    __table_args__ = (
        UniqueConstraint("projection", "event_id", name="uq_datalayer_applied_proj_event"),
    )

    projection: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    #: The position of the applied event (lets the store prune below the checkpoint).
    position: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


__all__ = [
    "DataLayerAppliedEventRecord",
    "DataLayerCheckpointRecord",
    "DataLayerReadModelRecord",
]
