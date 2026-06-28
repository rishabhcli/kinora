"""Repositories for consent policies and consent (proof) records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.compliance.db.models import ConsentPolicy, ConsentRecord
from app.compliance.enums import (
    ConsentAction,
    LawfulBasis,
    PolicyStatus,
    ProcessingPurpose,
)
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class ConsentPolicyRepo(BaseRepository):
    """Create and query versioned consent-policy documents."""

    async def create(
        self,
        *,
        purpose: ProcessingPurpose,
        version: int,
        title: str,
        body: str,
        body_hash: str,
        status: PolicyStatus = PolicyStatus.DRAFT,
        required: bool = False,
        effective_from: datetime | None = None,
        policy_id: str | None = None,
    ) -> ConsentPolicy:
        """Insert a new policy version row."""
        policy = ConsentPolicy(
            id=policy_id or new_id(),
            purpose=purpose,
            version=version,
            title=title,
            body=body,
            body_hash=body_hash,
            status=status,
            required=required,
            effective_from=effective_from,
        )
        self.session.add(policy)
        await self.session.flush()
        return policy

    async def get(self, policy_id: str) -> ConsentPolicy | None:
        """Fetch a policy by id."""
        return await self.session.get(ConsentPolicy, policy_id)

    async def get_version(self, purpose: ProcessingPurpose, version: int) -> ConsentPolicy | None:
        """Fetch the exact ``(purpose, version)`` policy."""
        stmt = select(ConsentPolicy).where(
            ConsentPolicy.purpose == purpose, ConsentPolicy.version == version
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def active_for(self, purpose: ProcessingPurpose) -> ConsentPolicy | None:
        """Return the single ACTIVE policy for a purpose (the one to consent to)."""
        stmt = select(ConsentPolicy).where(
            ConsentPolicy.purpose == purpose,
            ConsentPolicy.status == PolicyStatus.ACTIVE,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def latest_version(self, purpose: ProcessingPurpose) -> int:
        """Return the highest version number published for a purpose (0 if none)."""
        stmt = select(func.max(ConsentPolicy.version)).where(ConsentPolicy.purpose == purpose)
        result = (await self.session.execute(stmt)).scalar_one_or_none()
        return int(result) if result is not None else 0

    async def list_for_purpose(self, purpose: ProcessingPurpose) -> list[ConsentPolicy]:
        """All versions for a purpose, newest first."""
        stmt = (
            select(ConsentPolicy)
            .where(ConsentPolicy.purpose == purpose)
            .order_by(ConsentPolicy.version.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_active(self) -> list[ConsentPolicy]:
        """Every currently-active policy (one per purpose)."""
        stmt = select(ConsentPolicy).where(ConsentPolicy.status == PolicyStatus.ACTIVE)
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_status(self, policy_id: str, status: PolicyStatus) -> None:
        """Transition a policy's lifecycle status."""
        policy = await self.get(policy_id)
        if policy is not None:
            policy.status = status
            await self.session.flush()


class ConsentRecordRepo(BaseRepository):
    """Append and query the immutable consent-event log."""

    async def append(
        self,
        *,
        subject_id: str,
        purpose: ProcessingPurpose,
        action: ConsentAction,
        policy_id: str | None = None,
        policy_version: int | None = None,
        lawful_basis: LawfulBasis = LawfulBasis.CONSENT,
        source: dict[str, Any] | None = None,
        note: str | None = None,
        created_at: datetime | None = None,
    ) -> ConsentRecord:
        """Append one consent event (grant/withdraw). Never updates existing rows."""
        record = ConsentRecord(
            id=new_id(),
            subject_id=subject_id,
            purpose=purpose,
            action=action,
            policy_id=policy_id,
            policy_version=policy_version,
            lawful_basis=lawful_basis,
            source=source,
            note=note,
        )
        if created_at is not None:
            record.created_at = created_at
        self.session.add(record)
        await self.session.flush()
        return record

    async def latest(self, subject_id: str, purpose: ProcessingPurpose) -> ConsentRecord | None:
        """The most-recent event for a ``(subject, purpose)`` pair (defines state)."""
        stmt = (
            select(ConsentRecord)
            .where(
                ConsentRecord.subject_id == subject_id,
                ConsentRecord.purpose == purpose,
            )
            .order_by(ConsentRecord.seq.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def history(self, subject_id: str) -> list[ConsentRecord]:
        """Full consent history for a subject, oldest first (the proof trail)."""
        stmt = (
            select(ConsentRecord)
            .where(ConsentRecord.subject_id == subject_id)
            .order_by(ConsentRecord.seq.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def latest_per_purpose(self, subject_id: str) -> dict[ProcessingPurpose, ConsentRecord]:
        """The most-recent event for each purpose the subject ever touched."""
        history = await self.history(subject_id)
        latest: dict[ProcessingPurpose, ConsentRecord] = {}
        for record in history:  # oldest→newest, so last write wins
            latest[record.purpose] = record
        return latest


__all__ = ["ConsentPolicyRepo", "ConsentRecordRepo"]
