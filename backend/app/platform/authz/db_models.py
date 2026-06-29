"""ORM models for the unified authorization plane (additive; new tables only).

Two durable stores back the plane in production; both are *new* tables and touch
no existing schema (importing this module only adds tables to ``Base.metadata``):

* ``authz_relation_tuples`` — the Google-Zanzibar relationship facts
  (``object#relation@subject``). This is the durable backing for the in-memory
  :class:`~app.platform.authz.rebac.InMemoryTupleStore`; a
  :class:`~app.platform.authz.store_db.DbTupleStore` reads/writes it with the
  same protocol so the relation graph is identical in tests and production. The
  three indexes mirror the in-memory store's three: forward (object+relation),
  reverse (subject+relation+object_type), and incoming (subject as an object
  reference, for tuple-to-userset back-walks).
* ``authz_decision_log`` — the append-only decision audit. Every ``check`` the
  plane resolves can be persisted here (subject / action / resource / effect /
  reasons / digest), giving the unified "who was allowed/denied what, and why"
  that scattered checks never had.

The personal-owner / workspace / RBAC facts are *not* duplicated here — the plane
reads those through adapters over the existing tables. This store only holds the
plane's own native relationship grants and its audit trail, so it never competes
with the legacy schema as the source of truth.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class AuthzRelationTuple(StrIdMixin, CreatedAtMixin, Base):
    """A stored Zanzibar relation tuple: ``object#relation@subject``.

    ``object_type`` + ``object_id`` is the object; ``relation`` is the edge name;
    ``subject_type`` + ``subject_id`` is the subject, and ``subject_relation`` is
    set when the subject is a **userset** (``workspace:7#member``) rather than a
    concrete principal. The unique constraint makes (re)writing a tuple idempotent.
    """

    __tablename__ = "authz_relation_tuples"

    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Non-null => the subject is a userset (object#relation), not a concrete one.
    subject_relation: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Forward check: subjects of (object, relation).
        Index(
            "ix_authz_tuples_forward",
            "object_type",
            "object_id",
            "relation",
        ),
        # Reverse index: objects a concrete subject has (relation) on, by type.
        Index(
            "ix_authz_tuples_reverse",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
        ),
        # Incoming edges: rows whose subject names a given object (TTU back-walk).
        Index(
            "ix_authz_tuples_incoming",
            "subject_type",
            "subject_id",
        ),
        # Idempotent writes: one row per full tuple.
        Index(
            "uq_authz_tuples_full",
            "object_type",
            "object_id",
            "relation",
            "subject_type",
            "subject_id",
            "subject_relation",
            unique=True,
        ),
    )


class AuthzDecisionLogRow(StrIdMixin, Base):
    """An append-only audited authorization decision (the decision log)."""

    __tablename__ = "authz_decision_log"

    subject_ref: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_ref: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    effect: Mapped[str] = mapped_column(String(16), nullable=False)
    #: A newline-joined rendering of the reason trail (the "why").
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Stable hash over the decisive fields (for coalescing / tamper-evidence).
    digest: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


__all__ = ["AuthzDecisionLogRow", "AuthzRelationTuple"]
