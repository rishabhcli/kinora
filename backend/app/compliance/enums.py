"""Value-object enums for the compliance domain.

Every enum is a lowercase-string :class:`enum.StrEnum` so it serialises directly
to JSON and stores as a portable ``VARCHAR`` + ``CHECK`` constraint via
:func:`app.db.models.enums.str_enum` — the same convention the rest of the data
layer uses (no native Postgres ``ENUM`` types).

The vocabularies are deliberately small and stable; they are the words a Data
Protection Officer would use, mapped to the parts of Kinora that actually hold
personal data.
"""

from __future__ import annotations

import enum


class ProcessingPurpose(enum.StrEnum):
    """Why Kinora processes a piece of personal data (GDPR purpose limitation).

    Consent is *purpose-scoped*: a reader can consent to having their book
    adapted while withholding consent to model-training on their uploads.
    """

    #: Core product: ingest a book and generate its page-synced film (§4).
    ADAPTATION = "adaptation"
    #: Persist the reader's learned directing style across sessions (§8.6).
    PERSONALIZATION = "personalization"
    #: Aggregate, de-identified product analytics.
    ANALYTICS = "analytics"
    #: Use a reader's uploads / edits to improve the models. Off by default.
    MODEL_TRAINING = "model_training"
    #: Operational email (import-ready, DSAR receipts). Often "legitimate interest".
    TRANSACTIONAL_EMAIL = "transactional_email"
    #: Marketing / product-update email. Always consent-based.
    MARKETING_EMAIL = "marketing_email"


class LawfulBasis(enum.StrEnum):
    """The GDPR Art. 6(1) lawful basis under which a data class is processed."""

    CONSENT = "consent"  # Art. 6(1)(a)
    CONTRACT = "contract"  # Art. 6(1)(b)
    LEGAL_OBLIGATION = "legal_obligation"  # Art. 6(1)(c)
    VITAL_INTERESTS = "vital_interests"  # Art. 6(1)(d)
    PUBLIC_TASK = "public_task"  # Art. 6(1)(e)
    LEGITIMATE_INTERESTS = "legitimate_interests"  # Art. 6(1)(f)


class DataClass(enum.StrEnum):
    """A retention-governed category of personal data Kinora actually stores.

    These map to concrete tables / object-store prefixes; the retention engine
    attaches a TTL and lawful basis to each.
    """

    #: The account row (email, hashed password) — kept for the life of the account.
    ACCOUNT = "account"
    #: An uploaded source PDF in object storage — the reader's own content.
    UPLOADED_BOOK = "uploaded_book"
    #: Generated media (clips, keyframes, narration) derived from a book.
    GENERATED_MEDIA = "generated_media"
    #: Reading sessions / scroll trajectories — behavioural data.
    READING_SESSION = "reading_session"
    #: Learned directing-style preferences (§8.6).
    DIRECTING_PREFERENCE = "directing_preference"
    #: Security / audit logs — kept longer for accountability.
    AUDIT_LOG = "audit_log"
    #: Billing / budget records — kept for the statutory accounting period.
    BILLING_RECORD = "billing_record"


class ConsentAction(enum.StrEnum):
    """The append-only events that make up a subject's consent history."""

    GRANT = "grant"
    WITHDRAW = "withdraw"


class ConsentState(enum.StrEnum):
    """The *current* consent status for a (subject, purpose) pair.

    Derived by folding the append-only :class:`ConsentAction` log; never stored
    as mutable state (that would lose the proof trail GDPR Art. 7(1) requires).
    """

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    #: Never expressed an opinion — treated as no-consent (opt-in default).
    NEVER = "never"


class PolicyStatus(enum.StrEnum):
    """Lifecycle of a versioned consent-policy document."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class HoldStatus(enum.StrEnum):
    """Lifecycle of a legal hold."""

    ACTIVE = "active"
    LIFTED = "lifted"


class DSARKind(enum.StrEnum):
    """The data-subject right a DSAR exercises (GDPR Ch. III)."""

    ACCESS = "access"  # Art. 15 — a copy of their data (export)
    ERASURE = "erasure"  # Art. 17 — right to be forgotten
    RECTIFICATION = "rectification"  # Art. 16 — correct inaccurate data
    PORTABILITY = "portability"  # Art. 20 — machine-readable export
    RESTRICTION = "restriction"  # Art. 18 — restrict processing
    OBJECTION = "objection"  # Art. 21 — object to processing


class DSARState(enum.StrEnum):
    """The DSAR workflow state machine (kinora.md §12 engineering rigour).

    The happy path is ``received → verifying → in_progress → completed``; a request
    may be ``rejected`` (e.g. identity not verified) or ``cancelled`` by the subject.
    ``extended`` records the one-time GDPR Art. 12(3) extension of the deadline.
    """

    RECEIVED = "received"
    VERIFYING = "verifying"
    IN_PROGRESS = "in_progress"
    EXTENDED = "extended"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LedgerCategory(enum.StrEnum):
    """The source category of a consolidated compliance-ledger entry.

    The ledger aggregates events from across the platform into one tamper-evident
    chain, so each entry is tagged with where it came from.
    """

    CONSENT = "consent"
    RETENTION = "retention"
    DSAR = "dsar"
    LEGAL_HOLD = "legal_hold"
    POLICY = "policy"
    SECURITY = "security"
    MODERATION = "moderation"
    BILLING = "billing"


class PolicyDecision(enum.StrEnum):
    """The outcome of evaluating a policy-as-code rule."""

    ALLOW = "allow"
    DENY = "deny"
    #: Allowed but with an obligation the caller must satisfy (e.g. log it).
    ALLOW_WITH_OBLIGATION = "allow_with_obligation"


class RuleSeverity(enum.StrEnum):
    """How serious a failed compliance rule is, for report prioritisation."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


__all__ = [
    "ConsentAction",
    "ConsentState",
    "DSARKind",
    "DSARState",
    "DataClass",
    "HoldStatus",
    "LawfulBasis",
    "LedgerCategory",
    "PolicyDecision",
    "PolicyStatus",
    "ProcessingPurpose",
    "RuleSeverity",
]
