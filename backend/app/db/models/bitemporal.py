"""Bitemporal continuity facts + the canon audit log + branch registry (kinora.md §8).

Three additive tables that turn the canon from a uni-temporal versioned graph into a
**bitemporal knowledge graph** — without touching the existing ``entities`` /
``continuity_states`` tables (the legacy uni-temporal path keeps working unchanged):

* :class:`BitemporalState` — a continuity fact carrying **both** a valid-time beat interval
  (the story timeline, §8.5) **and** a transaction-time UTC interval (when the system
  believed it), plus a ``branch`` and a CRDT write-stamp. This is what makes "canon as of
  any past write" and conflict-free concurrent edits possible.
* :class:`CanonAudit` — an append-only, hash-chained record of every canon mutation
  (tamper-evident): each row's ``entry_hash`` covers its payload + the previous row's hash.
* :class:`CanonBranch` — the branch registry for FORK / DIFF / MERGE (a director edit can
  fork a line of canon, accumulate edits, then merge back to ``main``).

Beats are integer ordinals (see :mod:`app.db.models.entity`); transaction-time is
timezone-aware UTC. Both intervals are half-open ``[lo, hi)`` so versions tile without
overlap (exactly one active at any boundary).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, new_id
from app.db.models.enums import str_enum


class AuditAction(enum.StrEnum):
    """The kinds of canon mutation the audit log records (§8 + §7.2)."""

    ASSERT_FACT = "assert_fact"
    CORRECT_FACT = "correct_fact"
    RETIRE_FACT = "retire_fact"
    FORK_BRANCH = "fork_branch"
    MERGE_BRANCH = "merge_branch"
    UPSERT_ENTITY = "upsert_entity"


class BranchStatus(enum.StrEnum):
    """Lifecycle of a canon branch."""

    OPEN = "open"
    MERGED = "merged"
    ABANDONED = "abandoned"


class BitemporalState(StrIdMixin, CreatedAtMixin, Base):
    """A continuity fact scoped by VALID-time (beats) AND TRANSACTION-time (UTC).

    A *logical* fact is identified by ``fact_key`` (stable across corrections); each physical
    row is one *belief* of it. A correction closes the prior row's ``tx_to`` and inserts a
    successor sharing ``fact_key`` — so the transaction-time history is the full audit of
    what the canon believed and when. Forgetting (§8.5) closes ``valid_to_beat`` without
    deleting the row.
    """

    __tablename__ = "bitemporal_states"
    __table_args__ = (
        # The 4-D read filter: (book, branch, subject) + the validity/tx interval columns.
        Index(
            "ix_bitemporal_states_read",
            "book_id",
            "branch",
            "subject_entity_key",
            "valid_from_beat",
            "valid_to_beat",
        ),
        Index("ix_bitemporal_states_fact_key", "book_id", "fact_key"),
        Index("ix_bitemporal_states_branch", "book_id", "branch"),
        # The current-belief partial filter is expressed in queries (tx_to IS NULL); this
        # composite supports it without scanning superseded rows.
        Index("ix_bitemporal_states_tx", "book_id", "branch", "tx_to"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Stable identity of the *logical* fact across corrections (assigned on first assert).
    fact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    branch: Mapped[str] = mapped_column(String(128), nullable=False, default="main")

    subject_entity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    object_value: Mapped[str] = mapped_column(Text, nullable=False)

    # VALID time — the story timeline (beats); half-open [from, to).
    valid_from_beat: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_to_beat: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # TRANSACTION time — when the system believed it (UTC); half-open [from, to).
    tx_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tx_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # CRDT write-stamp so concurrent edits resolve deterministically (LWW). ``stamp_wall`` is
    # a millisecond UTC epoch (the HLC's physical anchor), so it must be 64-bit.
    stamp_wall: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stamp_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system")

    # {"page", "word_range": [...]} | {"page", "char_range": [...]}
    source_span: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class CanonAudit(StrIdMixin, CreatedAtMixin, Base):
    """One immutable, hash-chained record of a canon mutation (tamper-evident).

    ``entry_hash = H(prev_hash || canonical(payload))``. A verifier can re-hash the chain and
    detect any retroactive edit. ``seq`` is a per-book monotone sequence so the log replays
    deterministically. The payload is the projected before/after of the mutation.
    """

    __tablename__ = "canon_audit"
    __table_args__ = (
        UniqueConstraint("book_id", "seq", name="uq_canon_audit_book_id_seq"),
        Index("ix_canon_audit_book_seq", "book_id", "seq"),
        Index("ix_canon_audit_branch", "book_id", "branch"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    branch: Mapped[str] = mapped_column(String(128), nullable=False, default="main")
    action: Mapped[AuditAction] = mapped_column(
        str_enum(AuditAction, "canon_audit_action"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    #: The logical fact / entity / branch this mutation targets.
    target_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CanonBranch(StrIdMixin, CreatedAtMixin, Base):
    """A named line of canon (FORK / DIFF / MERGE).

    A branch is forked off a base coordinate ``(base_beat, base_tx)`` on a parent branch; its
    edits accumulate as ``bitemporal_states`` rows tagged with ``branch=name``. MERGE reads
    both sides and CRDT-merges back to the target.
    """

    __tablename__ = "canon_branches"
    __table_args__ = (
        UniqueConstraint("book_id", "name", name="uq_canon_branches_book_id_name"),
        Index("ix_canon_branches_book", "book_id"),
    )

    book_id: Mapped[str] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[BranchStatus] = mapped_column(
        str_enum(BranchStatus, "canon_branch_status"), nullable=False, default=BranchStatus.OPEN
    )
    base_beat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_tx: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def fresh_id() -> str:
        return new_id()


__all__ = [
    "AuditAction",
    "BitemporalState",
    "BranchStatus",
    "CanonAudit",
    "CanonBranch",
]
