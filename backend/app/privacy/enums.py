"""Value-object enums for the privacy / right-to-erasure subsystem.

Every enum is a lowercase-string :class:`enum.StrEnum` so it serialises directly
to JSON (the same convention the ``app.compliance`` governance layer uses). These
are the *enforcement* vocabulary — where personal data physically lives and how
it must be removed — as distinct from the governance vocabulary (lawful basis,
DSAR workflow) that the sibling :mod:`app.compliance` package owns.

This package is the **execution** half of the data-subject rights story:

* :mod:`app.compliance` decides *whether* and *for how long* data may be kept;
* :mod:`app.privacy` knows *which fields in which stores* hold a subject's data,
  *assembles* a portable copy of it (DSAR export), and *removes* it coherently
  across every store — hard-deleting the mutable stores while **crypto-erasing**
  the append-only event store + hash-chained audit log so their integrity proofs
  survive (right-to-erasure without breaking the chain).
"""

from __future__ import annotations

import enum


class StoreKind(enum.StrEnum):
    """A physical store that may hold a data subject's personal data.

    The data-map (:mod:`app.privacy.datamap`) tags every PII field with the store
    it lives in; the erasure orchestrator (:mod:`app.privacy.erasure`) dispatches a
    deletion strategy per store kind. The split matters: mutable relational rows
    and object blobs can be *hard-deleted*, but the append-only stores cannot —
    they must be *crypto-erased* to preserve their integrity proofs.
    """

    #: Relational rows (Postgres) — accounts, books, canon, sessions.
    RELATIONAL = "relational"
    #: Object storage (MinIO / S3) — clips, keyframes, narration, source PDFs.
    OBJECT_STORE = "object_store"
    #: The append-only domain event store (:mod:`app.eventsourcing`).
    EVENT_STORE = "event_store"
    #: The hash-chained, tamper-evident audit / compliance ledger.
    AUDIT_LOG = "audit_log"
    #: A cache / search index (derived, regenerable) — best-effort purge.
    DERIVED_INDEX = "derived_index"


class PIICategory(enum.StrEnum):
    """The kind of personal data a mapped field carries (GDPR Art. 4(1) / Art. 9).

    Drives both export grouping and the erasure strategy: a *direct identifier*
    (email) usually anonymises in place, while *user content* (an uploaded PDF)
    hard-deletes, and a *special category* triggers the strictest handling.
    """

    #: Directly identifies a person (email, display name, IP).
    DIRECT_IDENTIFIER = "direct_identifier"
    #: A credential or secret (password hash, API-key digest) — never exported raw.
    CREDENTIAL = "credential"
    #: Content the subject authored / uploaded (a source book, a comment).
    USER_CONTENT = "user_content"
    #: Media derived from the subject's content (clips, keyframes, narration).
    DERIVED_MEDIA = "derived_media"
    #: Behavioural / usage data (reading sessions, scroll trajectories).
    BEHAVIOURAL = "behavioural"
    #: Learned preferences (the §8.6 directing-style profile).
    PREFERENCE = "preference"
    #: GDPR Art. 9 special category (none expected today; reserved + strictest).
    SPECIAL_CATEGORY = "special_category"
    #: A pseudonymous identifier (subject id, anon analytics id) — links records.
    PSEUDONYMOUS_ID = "pseudonymous_id"


class ErasureStrategy(enum.StrEnum):
    """How a mapped field / store is cleared during right-to-erasure.

    The orchestrator picks a strategy per data-map field; an append-only store
    can only ever be ``CRYPTO_ERASE`` or ``REDACT`` (hard-deleting it would break
    the integrity chain), which the data-map validates at construction time.
    """

    #: Physically delete the rows / objects (mutable stores).
    HARD_DELETE = "hard_delete"
    #: Overwrite identifying values with a tombstone, keeping the row/structure
    #: (e.g. ``users.email`` -> ``erased+<token>@…`` so FKs and counts survive).
    ANONYMIZE = "anonymize"
    #: Destroy the per-subject encryption key so ciphertext is unrecoverable,
    #: leaving the (now-opaque) record in place — for append-only stores.
    CRYPTO_ERASE = "crypto_erase"
    #: Replace a field's value inside an append-only record with a redaction
    #: marker, re-deriving the integrity hash so the chain stays verifiable.
    REDACT = "redact"


class RetentionAction(enum.StrEnum):
    """What the retention engine decides for a data class at evaluation time."""

    #: Within its TTL — keep it.
    RETAIN = "retain"
    #: Past TTL and not held — eligible for deletion / anonymisation.
    EXPIRE = "expire"
    #: Past TTL but blocked by an active legal hold — must be kept.
    BLOCKED_BY_HOLD = "blocked_by_hold"


class ConsentStatus(enum.StrEnum):
    """The *current* consent status for a (subject, purpose) pair.

    Derived by folding the append-only consent record list; mirrors the
    governance layer's vocabulary so the two reconcile, but is computed locally so
    this package has no import dependency on :mod:`app.compliance`.
    """

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    #: Never expressed — opt-in default treats this as no-consent.
    NEVER = "never"


class ErasureState(enum.StrEnum):
    """The right-to-erasure orchestration state machine.

    Resumable: a run that crashes mid-way reopens in ``IN_PROGRESS`` and replays
    only the store-steps not yet ``done`` (idempotent per step).
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    #: Every store-step finished and the residual scan found nothing.
    COMPLETED = "completed"
    #: A legal hold blocked the run before any destructive step ran.
    BLOCKED = "blocked"
    #: A store-step failed irrecoverably; the run can be retried.
    FAILED = "failed"


class StepStatus(enum.StrEnum):
    """Per-store erasure-step status inside a resumable run."""

    PENDING = "pending"
    DONE = "done"
    #: Nothing to do for this store (subject had no data there).
    SKIPPED = "skipped"
    FAILED = "failed"


__all__ = [
    "ConsentStatus",
    "ErasureState",
    "ErasureStrategy",
    "PIICategory",
    "RetentionAction",
    "StepStatus",
    "StoreKind",
]
