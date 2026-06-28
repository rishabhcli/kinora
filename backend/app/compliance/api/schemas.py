"""Request/response DTOs for the compliance API.

Transport contracts only — inputs validate untrusted client data (``extra=forbid``),
outputs project service results into stable JSON shapes. The enums are reused
directly so the wire vocabulary matches the domain exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.compliance.enums import (
    ConsentState,
    DataClass,
    DSARKind,
    DSARState,
    LawfulBasis,
    LedgerCategory,
    PolicyDecision,
    ProcessingPurpose,
)

# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #


class ConsentMutationRequest(BaseModel):
    """Grant or withdraw consent for a purpose (the subject is the caller)."""

    model_config = ConfigDict(extra="forbid")

    purpose: ProcessingPurpose
    note: str | None = Field(default=None, max_length=500)


class PurposeConsentView(BaseModel):
    """The derived current consent for one purpose."""

    purpose: ProcessingPurpose
    state: ConsentState
    granted_version: int | None = None
    current_version: int | None = None
    is_granted: bool
    is_stale: bool
    decided_at: datetime | None = None


class ConsentSnapshotResponse(BaseModel):
    """Every purpose's current consent for a subject."""

    subject_id: str
    purposes: list[PurposeConsentView]


class ConsentRecordView(BaseModel):
    """One immutable proof-of-consent event."""

    id: str
    purpose: ProcessingPurpose
    action: str
    policy_version: int | None
    lawful_basis: LawfulBasis
    created_at: datetime
    note: str | None = None


class ConsentHistoryResponse(BaseModel):
    """The full append-only consent proof trail for a subject."""

    subject_id: str
    records: list[ConsentRecordView]


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


class RetentionRuleView(BaseModel):
    """A per-data-class retention rule (the retention schedule row)."""

    data_class: DataClass
    ttl_days: int | None
    lawful_basis: LawfulBasis
    expire_on_consent_withdrawal: bool
    description: str | None = None


class RetentionScheduleResponse(BaseModel):
    """The full retention schedule."""

    rules: list[RetentionRuleView]


# --------------------------------------------------------------------------- #
# Legal hold (admin / DPO surface)
# --------------------------------------------------------------------------- #


class PlaceHoldRequest(BaseModel):
    """Place a legal hold over a subject (optionally one data class)."""

    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=64)
    matter_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    data_class: DataClass | None = None


class LegalHoldView(BaseModel):
    """A legal hold record."""

    id: str
    subject_id: str | None
    data_class: DataClass | None
    status: str
    matter_id: str
    reason: str
    placed_by: str
    placed_at: datetime
    lifted_at: datetime | None = None


# --------------------------------------------------------------------------- #
# DSAR
# --------------------------------------------------------------------------- #


class OpenDSARRequest(BaseModel):
    """File a data-subject-access request."""

    model_config = ConfigDict(extra="forbid")

    kind: DSARKind
    note: str | None = Field(default=None, max_length=1000)


class DSARView(BaseModel):
    """A DSAR's state + deadlines."""

    id: str
    subject_id: str | None
    kind: DSARKind
    state: DSARState
    received_at: datetime
    due_at: datetime
    effective_due_at: datetime
    completed_at: datetime | None = None
    overdue: bool
    result: dict[str, Any] | None = None


class DSARListResponse(BaseModel):
    """Every DSAR for a subject."""

    requests: list[DSARView]


class DSARActionRequest(BaseModel):
    """An operator action on a DSAR (reject reason / extension reason)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------- #
# Ledger + report
# --------------------------------------------------------------------------- #


class LedgerEntryView(BaseModel):
    """One consolidated compliance-ledger entry."""

    seq: int
    category: LedgerCategory
    event: str
    subject_id: str | None
    actor_id: str
    created_at: datetime
    entry_hash: str
    prev_hash: str | None


class LedgerSliceResponse(BaseModel):
    """A subject's slice of the consolidated ledger."""

    subject_id: str
    entries: list[LedgerEntryView]


class LedgerVerifyResponse(BaseModel):
    """The result of re-hashing the ledger chain."""

    ok: bool
    entries: int
    broken_at: int | None = None
    reason: str | None = None


class RuleResultView(BaseModel):
    """One policy rule's outcome."""

    id: str
    title: str
    severity: str
    reference: str
    decision: PolicyDecision
    passed: bool
    message: str
    obligation: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class ComplianceReportResponse(BaseModel):
    """The consolidated compliance report for a subject."""

    subject_id: str
    generated_at: datetime
    decision: PolicyDecision
    is_compliant: bool
    obligations: list[str]
    summary: dict[str, Any]
    rules: list[RuleResultView]


__all__ = [
    "ComplianceReportResponse",
    "ConsentHistoryResponse",
    "ConsentMutationRequest",
    "ConsentRecordView",
    "ConsentSnapshotResponse",
    "DSARActionRequest",
    "DSARListResponse",
    "DSARView",
    "LedgerEntryView",
    "LedgerSliceResponse",
    "LedgerVerifyResponse",
    "LegalHoldView",
    "OpenDSARRequest",
    "PlaceHoldRequest",
    "PurposeConsentView",
    "RetentionRuleView",
    "RetentionScheduleResponse",
    "RuleResultView",
]
