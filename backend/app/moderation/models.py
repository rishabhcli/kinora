"""ORM models for the moderation subsystem (kinora.md §9, §10).

Five additive tables, all self-contained under the moderation domain so they can
ship without touching any existing model file:

* :class:`ModerationEvent` — one screening outcome (a verdict for one piece of
  content at one surface). The denormalised record the gate writes on every call.
* :class:`ModerationAuditEntry` — the **append-only, hash-chained** audit log:
  every event/decision/state-transition is one immutable, tamper-evident row
  (mirrors the canon audit log's discipline, §8).
* :class:`ReviewItem` — a human-review-queue row with the takedown/appeal **state
  machine** (:class:`~app.moderation.contracts.ReviewState`).
* :class:`ModerationTenantPolicy` — the persisted, configurable per-tenant policy.
* :class:`ViolationCounter` — per-actor rate-of-violation rolling tally for the
  repeat-offender escalation ladder.

Like the rest of the schema these use the portable VARCHAR+CHECK enum encoding
(:func:`app.db.models.enums.str_enum`) and the shared mixins, so Alembic
autogenerate stays stable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin, new_id
from app.db.models.enums import str_enum
from app.moderation.contracts import ReviewState, Surface
from app.moderation.taxonomy import Disposition, Severity


class ModerationEvent(StrIdMixin, CreatedAtMixin, Base):
    """One screening outcome — the verdict for one piece of content at one surface.

    Written by the gate on **every** call (allow included), so the audit trail and
    the eval harness see the full denominator, not just the blocks.
    """

    __tablename__ = "moderation_events"
    __table_args__ = (
        Index("ix_moderation_events_tenant", "tenant_id", "created_at"),
        Index("ix_moderation_events_book", "book_id"),
        Index("ix_moderation_events_actor", "user_id", "created_at"),
        Index("ix_moderation_events_decision", "tenant_id", "decision"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    surface: Mapped[Surface] = mapped_column(
        str_enum(Surface, "moderation_event_surface"), nullable=False
    )
    decision: Mapped[Disposition] = mapped_column(
        str_enum(Disposition, "moderation_event_decision"), nullable=False
    )
    severity: Mapped[int] = mapped_column(Integer, nullable=False, default=int(Severity.NONE))
    classifier: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Attribution (nullable FKs — a screening may precede any book/shot).
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), index=True, nullable=True
    )
    shot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # The categories + raw labels that drove the verdict (JSONB for the audit/UI).
    categories: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    labels: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)


class ModerationAuditEntry(StrIdMixin, CreatedAtMixin, Base):
    """One immutable, hash-chained moderation audit record (tamper-evident, §8 style).

    ``entry_hash = H(prev_hash || seq || action || actor || target || payload)``. The
    chain is **per-tenant** (``seq`` is monotone within a tenant) so a tenant's
    moderation history replays and verifies independently. Any retroactive edit to
    a past row breaks the chain at that point.
    """

    __tablename__ = "moderation_audit"
    __table_args__ = (
        UniqueConstraint("tenant_id", "seq", name="uq_moderation_audit_tenant_id_seq"),
        Index("ix_moderation_audit_tenant_seq", "tenant_id", "seq"),
        Index("ix_moderation_audit_target", "tenant_id", "target_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    #: The thing this entry is about: an event id, a review-item id, a policy id...
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ReviewItem(StrIdMixin, TimestampMixin, Base):
    """A human-review-queue row carrying the takedown/appeal state machine.

    Created when a screening FLAGs (or auto-takes-down) content. Reviewers move it
    through :class:`~app.moderation.contracts.ReviewState`; the owner can appeal a
    rejection. ``state_history`` is an append-only JSONB list of transitions so the
    item carries its own provenance even before the audit log is consulted.
    """

    __tablename__ = "moderation_review_items"
    __table_args__ = (
        Index("ix_moderation_review_items_queue", "tenant_id", "state", "created_at"),
        Index("ix_moderation_review_items_actor", "user_id"),
        Index("ix_moderation_review_items_book", "book_id"),
        Index("ix_moderation_review_items_assignee", "assignee_id", "state"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("moderation_events.id", ondelete="SET NULL"), nullable=True
    )
    surface: Mapped[Surface] = mapped_column(
        str_enum(Surface, "moderation_review_surface"), nullable=False
    )
    state: Mapped[ReviewState] = mapped_column(
        str_enum(ReviewState, "moderation_review_state"),
        nullable=False,
        default=ReviewState.PENDING,
    )
    decision: Mapped[Disposition] = mapped_column(
        str_enum(Disposition, "moderation_review_decision"), nullable=False
    )
    severity: Mapped[int] = mapped_column(Integer, nullable=False, default=int(Severity.NONE))
    categories: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=True
    )
    shot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    assignee_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolver_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Append-only [{state, actor, note, at}] transitions for self-contained provenance.
    state_history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    @staticmethod
    def fresh_id() -> str:
        return new_id()


class ModerationTenantPolicy(StrIdMixin, TimestampMixin, Base):
    """The persisted, configurable per-tenant moderation policy (§10).

    One row per tenant (unique ``tenant_id``). The blob is the serialised
    :class:`~app.moderation.tenant_policy.TenantPolicy`; the scalar columns are
    denormalised for cheap filtering/inspection without parsing the blob.
    """

    __tablename__ = "moderation_tenant_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_moderation_tenant_policies_tenant_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    strictness: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    fail_closed_on_degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    serve_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ViolationCounter(StrIdMixin, TimestampMixin, Base):
    """A per-actor rolling violation tally for repeat-offender escalation (§10).

    One row per (tenant, actor). The escalation service increments
    :attr:`window_count` within a rolling window and bumps :attr:`tier` when the
    rate crosses a threshold; :attr:`window_started_at` anchors the window so an
    old burst eventually decays.
    """

    __tablename__ = "moderation_violation_counters"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "actor_id", name="uq_moderation_violation_counters_tenant_id_actor_id"
        ),
        Index("ix_moderation_violation_counters_tier", "tenant_id", "tier"),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Lifetime count of recorded violations (block/takedown), never reset.
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Count inside the current rolling window (decays / resets per the window).
    window_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_violation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Escalation tier 0..N (0 = clean). Higher = harsher enforcement posture.
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: When set in the future, the actor is generation-suspended until this time.
    suspended_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "ModerationAuditEntry",
    "ModerationEvent",
    "ModerationTenantPolicy",
    "ReviewItem",
    "ViolationCounter",
]
