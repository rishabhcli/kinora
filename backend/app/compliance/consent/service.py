"""Consent management service — versioned policies + grant/withdraw + proof.

The consent *state* for a ``(subject, purpose)`` pair is never stored as mutable
data; it is **derived** by folding the append-only :class:`ConsentRecord` log
(the most-recent event wins). This is what makes the proof trail GDPR Art. 7(1)
demands: every grant and withdrawal is preserved with its policy version, lawful
basis, and capture metadata, and the current answer is always reconstructable.

Policies are versioned. When a policy body changes, a new version is published
and previously-granted consent to an *older* version is reported as **stale**:
the subject must re-consent. ``consent_state`` answers "is there a live grant?"
while :meth:`snapshot` answers "and is it to the current policy version?".

Every mutation is mirrored into the consolidated compliance ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.compliance.clock import Clock, system_clock
from app.compliance.consent.policy import (
    DEFAULT_PURPOSE_CATALOG,
    PolicyDraft,
    PurposeSpec,
    body_hash,
)
from app.compliance.db.models import ConsentPolicy
from app.compliance.enums import (
    ConsentAction,
    ConsentState,
    LawfulBasis,
    LedgerCategory,
    PolicyStatus,
    ProcessingPurpose,
)
from app.compliance.errors import ConflictError, ConsentRequiredError, NotFoundError
from app.compliance.ledger.service import ComplianceLedger
from app.compliance.repositories.consent import ConsentPolicyRepo, ConsentRecordRepo
from app.core.logging import get_logger

logger = get_logger("app.compliance.consent")


@dataclass(frozen=True)
class PurposeConsent:
    """The derived current consent for one purpose."""

    purpose: ProcessingPurpose
    state: ConsentState
    #: The policy version the live grant is against (None if never granted).
    granted_version: int | None = None
    #: The active policy version the subject *should* be on (None if no policy).
    current_version: int | None = None
    decided_at: datetime | None = None

    @property
    def is_granted(self) -> bool:
        """True when there is a live (non-withdrawn) grant for this purpose."""
        return self.state == ConsentState.GRANTED

    @property
    def is_stale(self) -> bool:
        """True when granted, but against an older policy version (needs re-consent)."""
        return (
            self.state == ConsentState.GRANTED
            and self.current_version is not None
            and self.granted_version != self.current_version
        )


@dataclass(frozen=True)
class ConsentSnapshot:
    """The full per-purpose consent picture for a subject."""

    subject_id: str
    purposes: tuple[PurposeConsent, ...]

    def for_purpose(self, purpose: ProcessingPurpose) -> PurposeConsent:
        """Return the consent for one purpose (NEVER if untouched)."""
        for entry in self.purposes:
            if entry.purpose == purpose:
                return entry
        return PurposeConsent(purpose=purpose, state=ConsentState.NEVER)


class ConsentService:
    """Publish/activate policies and grant/withdraw/inspect subject consent."""

    def __init__(
        self,
        policies: ConsentPolicyRepo,
        records: ConsentRecordRepo,
        ledger: ComplianceLedger,
        *,
        clock: Clock = system_clock,
    ) -> None:
        self._policies = policies
        self._records = records
        self._ledger = ledger
        self._clock = clock

    # --- policy lifecycle --------------------------------------------------- #

    async def publish(self, draft: PolicyDraft) -> ConsentPolicy:
        """Publish the next DRAFT version of a policy for a purpose."""
        next_version = await self._policies.latest_version(draft.purpose) + 1
        policy = await self._policies.create(
            purpose=draft.purpose,
            version=next_version,
            title=draft.title,
            body=draft.body,
            body_hash=body_hash(draft.body),
            status=PolicyStatus.DRAFT,
            required=draft.required,
        )
        await self._ledger.record(
            category=LedgerCategory.POLICY,
            event="policy.published",
            payload={
                "policy_id": policy.id,
                "purpose": draft.purpose.value,
                "version": next_version,
            },
        )
        return policy

    async def activate(self, policy_id: str) -> ConsentPolicy:
        """Activate a DRAFT policy; supersede any currently-active version."""
        policy = await self._policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"consent policy {policy_id!r} not found")
        if policy.status == PolicyStatus.ACTIVE:
            return policy
        if policy.status == PolicyStatus.SUPERSEDED:
            raise ConflictError("cannot re-activate a superseded policy version")
        current = await self._policies.active_for(policy.purpose)
        if current is not None and current.id != policy.id:
            current.status = PolicyStatus.SUPERSEDED
            current.effective_to = self._clock()
        policy.status = PolicyStatus.ACTIVE
        policy.effective_from = self._clock()
        await self._policies.session.flush()
        await self._ledger.record(
            category=LedgerCategory.POLICY,
            event="policy.activated",
            payload={
                "policy_id": policy.id,
                "purpose": policy.purpose.value,
                "version": policy.version,
            },
        )
        return policy

    async def seed_catalog(
        self, catalog: tuple[PurposeSpec, ...] = DEFAULT_PURPOSE_CATALOG
    ) -> list[ConsentPolicy]:
        """Idempotently publish + activate v1 of every catalog purpose missing one.

        Safe to call on every boot: purposes that already have an active policy
        are skipped, so re-seeding never duplicates versions.
        """
        published: list[ConsentPolicy] = []
        for spec in catalog:
            if await self._policies.active_for(spec.purpose) is not None:
                continue
            policy = await self.publish(
                PolicyDraft(
                    purpose=spec.purpose,
                    title=spec.title,
                    body=spec.body,
                    required=spec.required,
                )
            )
            await self.activate(policy.id)
            published.append(policy)
        return published

    # --- grant / withdraw --------------------------------------------------- #

    async def grant(
        self,
        *,
        subject_id: str,
        purpose: ProcessingPurpose,
        source: dict[str, Any] | None = None,
        lawful_basis: LawfulBasis = LawfulBasis.CONSENT,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> PurposeConsent:
        """Record a consent grant against the currently-active policy version."""
        policy = await self._policies.active_for(purpose)
        await self._records.append(
            subject_id=subject_id,
            purpose=purpose,
            action=ConsentAction.GRANT,
            policy_id=policy.id if policy else None,
            policy_version=policy.version if policy else None,
            lawful_basis=lawful_basis,
            source=source,
            note=note,
            created_at=self._clock(),
        )
        await self._ledger.record(
            category=LedgerCategory.CONSENT,
            event="consent.granted",
            subject_id=subject_id,
            actor_id=actor_id or subject_id,
            payload={
                "purpose": purpose.value,
                "policy_version": policy.version if policy else None,
                "lawful_basis": lawful_basis.value,
            },
        )
        return await self.consent_for(subject_id, purpose)

    async def withdraw(
        self,
        *,
        subject_id: str,
        purpose: ProcessingPurpose,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> PurposeConsent:
        """Record a consent withdrawal (the easy-as-giving requirement, Art. 7(3))."""
        latest = await self._records.latest(subject_id, purpose)
        await self._records.append(
            subject_id=subject_id,
            purpose=purpose,
            action=ConsentAction.WITHDRAW,
            policy_id=latest.policy_id if latest else None,
            policy_version=latest.policy_version if latest else None,
            note=note,
            created_at=self._clock(),
        )
        await self._ledger.record(
            category=LedgerCategory.CONSENT,
            event="consent.withdrawn",
            subject_id=subject_id,
            actor_id=actor_id or subject_id,
            payload={"purpose": purpose.value},
        )
        return await self.consent_for(subject_id, purpose)

    # --- reads -------------------------------------------------------------- #

    async def consent_for(self, subject_id: str, purpose: ProcessingPurpose) -> PurposeConsent:
        """Derive the current consent for one ``(subject, purpose)``."""
        latest = await self._records.latest(subject_id, purpose)
        active = await self._policies.active_for(purpose)
        current_version = active.version if active else None
        if latest is None:
            return PurposeConsent(
                purpose=purpose, state=ConsentState.NEVER, current_version=current_version
            )
        state = (
            ConsentState.GRANTED if latest.action == ConsentAction.GRANT else ConsentState.WITHDRAWN
        )
        return PurposeConsent(
            purpose=purpose,
            state=state,
            granted_version=latest.policy_version if state == ConsentState.GRANTED else None,
            current_version=current_version,
            decided_at=latest.created_at,
        )

    async def snapshot(self, subject_id: str) -> ConsentSnapshot:
        """The full per-purpose consent picture for a subject (every purpose)."""
        purposes: list[PurposeConsent] = [
            await self.consent_for(subject_id, purpose) for purpose in ProcessingPurpose
        ]
        return ConsentSnapshot(subject_id=subject_id, purposes=tuple(purposes))

    async def has_consent(self, subject_id: str, purpose: ProcessingPurpose) -> bool:
        """True iff the subject currently has a live grant for the purpose."""
        return (await self.consent_for(subject_id, purpose)).is_granted

    async def require(self, subject_id: str, purpose: ProcessingPurpose) -> None:
        """Raise :class:`ConsentRequiredError` unless the subject has consented.

        The single gate other subsystems call before processing personal data for
        a consent-based purpose.
        """
        if not await self.has_consent(subject_id, purpose):
            raise ConsentRequiredError(purpose.value, subject_id)


__all__ = ["ConsentService", "ConsentSnapshot", "PurposeConsent"]
