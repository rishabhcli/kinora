"""Consent tracking (purpose-scoped, append-only, proof-bearing).

GDPR Art. 7(1) requires the controller to be able to *demonstrate* that a subject
consented. So consent is never stored as mutable state — it is an **append-only
log** of grant/withdraw records, and the *current* status for a (subject, purpose)
pair is derived by folding that log (last action wins). Each record carries the
policy version and a timestamp, so the proof trail is reconstructable.

This is the privacy-subsystem's own lightweight consent ledger (kept local so the
package stands alone); it reconciles with — but does not import — the richer
:mod:`app.compliance.consent` governance store. The right-to-erasure orchestrator
and the retention engine read consent here to decide, e.g., that withdrawing
``model_training`` consent makes the training-derived data class expiry-eligible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.privacy.clock import Clock, ensure_utc, system_clock
from app.privacy.enums import ConsentStatus


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """One append-only consent event (a grant or a withdrawal).

    ``granted=True`` is a grant, ``False`` a withdrawal; folding the records for a
    purpose by ``at`` (last wins) yields the current :class:`ConsentStatus`.
    """

    subject_id: str
    purpose: str
    granted: bool
    at: datetime
    policy_version: str = "v1"
    source: str = "subject"  # who recorded it (subject | admin | import)

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_utc(self.at))


@dataclass(frozen=True, slots=True)
class PurposeConsent:
    """The derived, current consent for one (subject, purpose) pair."""

    purpose: str
    status: ConsentStatus
    last_action_at: datetime | None
    policy_version: str | None


@dataclass
class ConsentTracker:
    """An in-memory, append-only consent ledger with derived current status.

    Deterministic and infra-free: production can persist the same records, but the
    folding logic — the part that decides whether consent is currently held — lives
    here so it is unit-testable in isolation.
    """

    _records: list[ConsentRecord] = field(default_factory=list)
    clock: Clock = system_clock

    @classmethod
    def from_records(
        cls, records: Iterable[ConsentRecord], *, clock: Clock = system_clock
    ) -> ConsentTracker:
        """Build a tracker seeded with existing records (e.g. loaded from a store)."""
        t = cls(clock=clock)
        t._records = list(records)
        return t

    def record(
        self,
        *,
        subject_id: str,
        purpose: str,
        granted: bool,
        policy_version: str = "v1",
        source: str = "subject",
        at: datetime | None = None,
    ) -> ConsentRecord:
        """Append a grant/withdraw record and return it."""
        rec = ConsentRecord(
            subject_id=subject_id,
            purpose=purpose,
            granted=granted,
            at=at or self.clock(),
            policy_version=policy_version,
            source=source,
        )
        self._records.append(rec)
        return rec

    def grant(self, *, subject_id: str, purpose: str, **kw: object) -> ConsentRecord:
        """Record a consent grant."""
        return self.record(subject_id=subject_id, purpose=purpose, granted=True, **kw)  # type: ignore[arg-type]

    def withdraw(self, *, subject_id: str, purpose: str, **kw: object) -> ConsentRecord:
        """Record a consent withdrawal."""
        return self.record(subject_id=subject_id, purpose=purpose, granted=False, **kw)  # type: ignore[arg-type]

    def records_for(self, subject_id: str) -> list[ConsentRecord]:
        """Every consent record for a subject, oldest first (the proof trail)."""
        return sorted(
            (r for r in self._records if r.subject_id == subject_id),
            key=lambda r: r.at,
        )

    def status(self, *, subject_id: str, purpose: str) -> PurposeConsent:
        """Fold the log to the current status for one (subject, purpose) pair."""
        relevant = [
            r
            for r in self._records
            if r.subject_id == subject_id and r.purpose == purpose
        ]
        if not relevant:
            return PurposeConsent(
                purpose=purpose,
                status=ConsentStatus.NEVER,
                last_action_at=None,
                policy_version=None,
            )
        last = max(relevant, key=lambda r: r.at)
        return PurposeConsent(
            purpose=purpose,
            status=ConsentStatus.GRANTED if last.granted else ConsentStatus.WITHDRAWN,
            last_action_at=last.at,
            policy_version=last.policy_version,
        )

    def snapshot(
        self, *, subject_id: str, purposes: Sequence[str]
    ) -> list[PurposeConsent]:
        """Current status across a list of purposes (for the DSAR export bundle)."""
        return [self.status(subject_id=subject_id, purpose=p) for p in purposes]

    def has_consent(self, *, subject_id: str, purpose: str) -> bool:
        """Whether the subject currently consents to ``purpose`` (opt-in default)."""
        return self.status(subject_id=subject_id, purpose=purpose).status is ConsentStatus.GRANTED

    def purge_subject(self, subject_id: str) -> int:
        """Drop a subject's consent records (called during erasure); return count.

        Idempotent: a second call removes nothing.
        """
        before = len(self._records)
        self._records = [r for r in self._records if r.subject_id != subject_id]
        return before - len(self._records)


__all__ = [
    "ConsentRecord",
    "ConsentTracker",
    "PurposeConsent",
]
