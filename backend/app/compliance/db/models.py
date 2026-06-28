"""Compliance ORM models — seven additive tables.

These tables are self-contained: they reference ``users.id`` (the data subject)
with ``ON DELETE SET NULL`` so the **proof trail survives account deletion**
(GDPR Art. 7(1) requires demonstrating consent even after the fact, and the
audit ledger is by definition append-only). No existing table is modified.

Tables:

* :class:`ConsentPolicy`  — a versioned policy document for one purpose.
* :class:`ConsentRecord`  — append-only grant/withdraw proof events.
* :class:`RetentionRule`  — per-data-class TTL + lawful basis.
* :class:`LegalHold`      — a hold suspending retention/erasure for a subject.
* :class:`DSARRequest`    — a data-subject-access-request row + its state.
* :class:`DSAREvent`      — append-only DSAR state-transition log.
* :class:`ComplianceLedgerEntry` — the consolidated hash-chained audit ledger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.compliance.enums import (
    ConsentAction,
    DataClass,
    DSARKind,
    DSARState,
    HoldStatus,
    LawfulBasis,
    LedgerCategory,
    PolicyStatus,
    ProcessingPurpose,
)
from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin
from app.db.models.enums import str_enum

# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #


class ConsentPolicy(StrIdMixin, TimestampMixin, Base):
    """A versioned consent-policy document for a single processing purpose.

    A reader consents *to a specific version* of a policy; when the policy text
    changes (a new purpose, a broader scope), a new ``version`` is published and
    existing consents become stale until re-granted. ``(purpose, version)`` is
    unique; ``status`` tracks draft → active → superseded.
    """

    __tablename__ = "consent_policies"
    __table_args__ = (
        UniqueConstraint("purpose", "version", name="uq_consent_policies_purpose_version"),
        Index("ix_consent_policies_purpose_status", "purpose", "status"),
    )

    purpose: Mapped[ProcessingPurpose] = mapped_column(
        str_enum(ProcessingPurpose, "consent_policy_purpose"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        str_enum(PolicyStatus, "consent_policy_status"),
        nullable=False,
        default=PolicyStatus.DRAFT,
    )
    #: Human-readable title shown in the consent UI.
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    #: The full policy body the subject is agreeing to.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 of the canonicalised body — what proof records pin to (immutable text).
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: When this version takes effect / stops being the active one (UTC).
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Whether granting this consent is mandatory to use the product.
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ConsentRecord(StrIdMixin, CreatedAtMixin, Base):
    """One append-only consent event — the proof trail (GDPR Art. 7(1)).

    Records are never updated or deleted; the *current* state for a
    ``(subject, purpose)`` pair is derived by folding the most-recent event.
    ``subject_id`` is ``SET NULL`` on user deletion so the demonstrable record of
    *what* was consented survives even after the account is erased.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_records_subject_purpose", "subject_id", "purpose", "seq"),
        Index("ix_consent_records_policy", "policy_id"),
    )

    #: Monotone insertion order — the deterministic tiebreak for "latest event"
    #: even when two events share a ``created_at`` (e.g. an injected fixed clock).
    #: Server-generated identity so the application never assigns it.
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), unique=True, nullable=False
    )
    subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[ProcessingPurpose] = mapped_column(
        str_enum(ProcessingPurpose, "consent_record_purpose"), nullable=False
    )
    action: Mapped[ConsentAction] = mapped_column(
        str_enum(ConsentAction, "consent_record_action"), nullable=False
    )
    policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("consent_policies.id", ondelete="SET NULL"), nullable=True
    )
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lawful_basis: Mapped[LawfulBasis] = mapped_column(
        str_enum(LawfulBasis, "consent_record_basis"),
        nullable=False,
        default=LawfulBasis.CONSENT,
    )
    #: Proof metadata: the source/IP/user-agent/locale captured at grant time.
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Optional free-text note (e.g. "withdrawn via Settings panel").
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


class RetentionRule(StrIdMixin, TimestampMixin, Base):
    """A retention policy for one data class — TTL + lawful basis.

    ``ttl_days`` of ``NULL`` means "retain for the life of the account" (e.g. the
    account row itself). ``data_class`` is unique: there is exactly one active
    rule per class. The lawful basis pins *why* the class is retained, which
    drives whether a withdrawal of consent forces expiry.
    """

    __tablename__ = "retention_rules"
    __table_args__ = (UniqueConstraint("data_class", name="uq_retention_rules_data_class"),)

    data_class: Mapped[DataClass] = mapped_column(
        str_enum(DataClass, "retention_rule_data_class"), nullable=False
    )
    #: Days to retain after the data's reference time; NULL == keep indefinitely.
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lawful_basis: Mapped[LawfulBasis] = mapped_column(
        str_enum(LawfulBasis, "retention_rule_basis"), nullable=False
    )
    #: If true, withdrawing the relevant consent makes the data an immediate
    #: expiry candidate (consent is the *only* basis for keeping it).
    expire_on_consent_withdrawal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Legal hold
# --------------------------------------------------------------------------- #


class LegalHold(StrIdMixin, TimestampMixin, Base):
    """A legal hold suspending retention/erasure for a subject.

    A hold may be scoped to a single ``data_class`` (NULL == all of the subject's
    data). While ``status == active`` the retention engine excludes the subject's
    matching data from expiry candidates and the DSAR machine refuses erasure.
    """

    __tablename__ = "legal_holds"
    __table_args__ = (
        Index("ix_legal_holds_subject_status", "subject_id", "status"),
        Index("ix_legal_holds_matter", "matter_id"),
    )

    subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Optional class scope; NULL means the hold covers ALL of the subject's data.
    data_class: Mapped[DataClass | None] = mapped_column(
        str_enum(DataClass, "legal_hold_data_class"), nullable=True
    )
    status: Mapped[HoldStatus] = mapped_column(
        str_enum(HoldStatus, "legal_hold_status"), nullable=False, default=HoldStatus.ACTIVE
    )
    #: The matter / case / ticket the hold is associated with.
    matter_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# DSAR
# --------------------------------------------------------------------------- #


class DSARRequest(StrIdMixin, TimestampMixin, Base):
    """A data-subject-access-request and its current workflow state.

    The deadline columns implement GDPR Art. 12(3): ``due_at`` is one month from
    receipt; ``extended_due_at`` records the one-time two-month extension. The
    full transition history lives in :class:`DSAREvent`.
    """

    __tablename__ = "dsar_requests"
    __table_args__ = (
        Index("ix_dsar_requests_subject_state", "subject_id", "state"),
        Index("ix_dsar_requests_due", "state", "due_at"),
    )

    subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Email captured at request time so the request survives account erasure.
    subject_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    kind: Mapped[DSARKind] = mapped_column(str_enum(DSARKind, "dsar_request_kind"), nullable=False)
    state: Mapped[DSARState] = mapped_column(
        str_enum(DSARState, "dsar_request_state"), nullable=False, default=DSARState.RECEIVED
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Statutory deadline (one month from receipt).
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Extended deadline once Art. 12(3) extension is applied (else NULL).
    extended_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The fulfilment result (export bundle ref, erased-class summary, etc.).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class DSAREvent(StrIdMixin, CreatedAtMixin, Base):
    """One append-only DSAR state-transition record."""

    __tablename__ = "dsar_events"
    __table_args__ = (Index("ix_dsar_events_request", "request_id", "seq"),)

    #: Monotone insertion order — the deterministic ordering for the transition
    #: history even when events share a ``created_at`` (injected fixed clock).
    #: Server-generated identity so the application never assigns it.
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), unique=True, nullable=False
    )
    request_id: Mapped[str] = mapped_column(
        ForeignKey("dsar_requests.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[DSARState | None] = mapped_column(
        str_enum(DSARState, "dsar_event_from_state"), nullable=True
    )
    to_state: Mapped[DSARState] = mapped_column(
        str_enum(DSARState, "dsar_event_to_state"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


# --------------------------------------------------------------------------- #
# Consolidated compliance ledger
# --------------------------------------------------------------------------- #


class ComplianceLedgerEntry(StrIdMixin, CreatedAtMixin, Base):
    """One immutable, hash-chained entry in the consolidated compliance ledger.

    ``entry_hash = sha256(prev_hash || canonical_json(payload_core))``. ``seq`` is a
    global monotone sequence so the chain replays deterministically and any
    retroactive edit is detectable (mirrors :class:`app.db.models.bitemporal.CanonAudit`,
    but consolidated across *all* compliance categories, not per-book canon).
    """

    __tablename__ = "compliance_ledger"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_compliance_ledger_seq"),
        Index("ix_compliance_ledger_category", "category", "created_at"),
        Index("ix_compliance_ledger_subject", "subject_id", "created_at"),
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[LedgerCategory] = mapped_column(
        str_enum(LedgerCategory, "compliance_ledger_category"), nullable=False
    )
    #: The event verb, free-form within a category (e.g. "consent.granted").
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The data subject this entry concerns (NULL for system-wide events). No FK
    #: so the ledger row outlives the subject (append-only accountability log).
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "ComplianceLedgerEntry",
    "ConsentPolicy",
    "ConsentRecord",
    "DSAREvent",
    "DSARRequest",
    "LegalHold",
    "RetentionRule",
]
