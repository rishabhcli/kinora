"""The default retention schedule — TTL + lawful basis per data class.

Each :class:`RetentionSpec` maps a :class:`~app.compliance.enums.DataClass` to a
retention period and the GDPR Art. 6 lawful basis under which Kinora keeps it.
``ttl_days is None`` means "retain for the life of the account" (e.g. the account
row itself, kept under *contract*). ``expire_on_consent_withdrawal`` marks classes
whose only basis is *consent*, so withdrawing it makes the data an immediate
expiry candidate (storage-limitation, Art. 5(1)(e)).

A deployment seeds this baseline into ``retention_rules`` on first boot and can
override any class via the API.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.compliance.enums import DataClass, LawfulBasis, ProcessingPurpose


@dataclass(frozen=True)
class RetentionSpec:
    """The shipped retention rule for one data class."""

    data_class: DataClass
    ttl_days: int | None
    lawful_basis: LawfulBasis
    expire_on_consent_withdrawal: bool = False
    description: str = ""
    #: The consent purpose tied to this class (for the consent→retention link).
    consent_purpose: ProcessingPurpose | None = None


#: The shipped baseline retention schedule.
DEFAULT_RETENTION_SCHEDULE: tuple[RetentionSpec, ...] = (
    RetentionSpec(
        data_class=DataClass.ACCOUNT,
        ttl_days=None,  # life of the account
        lawful_basis=LawfulBasis.CONTRACT,
        description="Account credentials kept for the life of the account (contract).",
    ),
    RetentionSpec(
        data_class=DataClass.UPLOADED_BOOK,
        ttl_days=None,  # the reader's own content, kept until they remove it
        lawful_basis=LawfulBasis.CONSENT,
        expire_on_consent_withdrawal=True,
        consent_purpose=ProcessingPurpose.ADAPTATION,
        description="Uploaded source PDFs; removed when adaptation consent is withdrawn.",
    ),
    RetentionSpec(
        data_class=DataClass.GENERATED_MEDIA,
        ttl_days=365,
        lawful_basis=LawfulBasis.CONSENT,
        expire_on_consent_withdrawal=True,
        consent_purpose=ProcessingPurpose.ADAPTATION,
        description="Generated clips/keyframes/narration; expire 1y after last use.",
    ),
    RetentionSpec(
        data_class=DataClass.READING_SESSION,
        ttl_days=90,
        lawful_basis=LawfulBasis.LEGITIMATE_INTERESTS,
        description="Reading sessions / scroll trajectories; 90-day rolling window.",
    ),
    RetentionSpec(
        data_class=DataClass.DIRECTING_PREFERENCE,
        ttl_days=None,
        lawful_basis=LawfulBasis.CONSENT,
        expire_on_consent_withdrawal=True,
        consent_purpose=ProcessingPurpose.PERSONALIZATION,
        description="Learned directing-style priors; kept while personalization consent lasts.",
    ),
    RetentionSpec(
        data_class=DataClass.AUDIT_LOG,
        ttl_days=2190,  # 6 years — accountability / security records
        lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
        description="Security/audit logs kept 6y for accountability obligations.",
    ),
    RetentionSpec(
        data_class=DataClass.BILLING_RECORD,
        ttl_days=2555,  # 7 years — statutory accounting period
        lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
        description="Billing/budget records kept 7y for the statutory accounting period.",
    ),
)

#: Convenience lookup by data class.
SCHEDULE_BY_CLASS: dict[DataClass, RetentionSpec] = {
    spec.data_class: spec for spec in DEFAULT_RETENTION_SCHEDULE
}


__all__ = ["DEFAULT_RETENTION_SCHEDULE", "SCHEDULE_BY_CLASS", "RetentionSpec"]
