"""ORM models for the durable audit log — two additive, append-only tables.

* :class:`AuditLogEntry`  — one immutable, hash-chained audit record. The
  application assigns ``seq`` (so the hash chain is deterministic and a verifier
  can re-derive it); the unique ``(seq)`` constraint serialises concurrent
  appenders — the loser's flush raises ``IntegrityError`` and retries.
* :class:`AuditCheckpoint` — one sealed Merkle checkpoint over a contiguous
  segment of entries (a compact, publishable commitment).

Both tables are self-contained: they hold ``actor_id`` / ``target_id`` as opaque
strings (no FK) so the **proof trail survives** deletion of whatever they point
at — an audit log that cascades away with its subject is not an audit log. No
existing table is modified. Enums are portable VARCHAR + named CHECK
(``native_enum=False``), matching the rest of the schema.

This module is imported for its side effect (registering the tables on
``Base.metadata``) by :mod:`app.audit.registry`; it is *not* added to the shared
``app.db.models`` registry to keep the subsystem self-contained and additive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
)
from app.db.base import Base, CreatedAtMixin, StrIdMixin
from app.db.models.enums import str_enum


class AuditLogEntry(StrIdMixin, CreatedAtMixin, Base):
    """One immutable, hash-chained audit record (append-only)."""

    __tablename__ = "audit_log_entries"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_log_entries_seq"),
        UniqueConstraint("entry_hash", name="uq_audit_log_entries_entry_hash"),
        Index("ix_audit_log_entries_actor", "actor_id", "seq"),
        Index("ix_audit_log_entries_target", "target_type", "target_id", "seq"),
        Index("ix_audit_log_entries_correlation", "correlation_id", "seq"),
        Index("ix_audit_log_entries_category_action", "category", "action", "seq"),
        Index("ix_audit_log_entries_occurred_at", "occurred_at"),
    )

    #: Monotone, application-assigned chain position (1-based). Unique so two
    #: concurrent appenders cannot both claim the same slot.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: The event's own logical timestamp (UTC), distinct from ``created_at``.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    category: Mapped[AuditCategory] = mapped_column(
        str_enum(AuditCategory, "audit_category"), nullable=False
    )
    action: Mapped[AuditAction] = mapped_column(
        str_enum(AuditAction, "audit_action"), nullable=False
    )
    severity: Mapped[AuditSeverity] = mapped_column(
        str_enum(AuditSeverity, "audit_severity"), nullable=False
    )
    actor_kind: Mapped[AuditActorKind] = mapped_column(
        str_enum(AuditActorKind, "audit_actor_kind"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)

    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Redacted state snapshots / structured detail (PII already committed-not-stored).
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: The hash-chain fields. ``prev_hash`` of the genesis entry is the all-zero
    #: sentinel; ``entry_hash`` = sha256(prev_hash || canonical_json(core)).
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: True once this entry's segment has been sealed under a Merkle checkpoint.
    sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AuditCheckpoint(StrIdMixin, CreatedAtMixin, Base):
    """One sealed Merkle checkpoint over a contiguous segment of entries."""

    __tablename__ = "audit_checkpoints"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_checkpoints_seq"),
        Index("ix_audit_checkpoints_range", "from_seq", "to_seq"),
    )

    #: Checkpoint ordinal (1-based) — the chain-of-checkpoints position.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = ["AuditCheckpoint", "AuditLogEntry"]
