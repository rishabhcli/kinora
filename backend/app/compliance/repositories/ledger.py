"""Repository for the consolidated, hash-chained compliance ledger.

The ledger is **append-only** and globally sequenced. ``next_seq`` and ``tail``
are read under the caller's transaction; concurrent appenders are serialised by
the unique ``(seq)`` constraint — a racing insert fails the constraint and the
caller retries (the service layer wraps appends with that retry).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.compliance.db.models import ComplianceLedgerEntry
from app.compliance.enums import LedgerCategory
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class ComplianceLedgerRepo(BaseRepository):
    """Append and verify entries in the consolidated compliance ledger."""

    async def tail(self) -> ComplianceLedgerEntry | None:
        """The most-recent entry (whose ``entry_hash`` the next entry chains onto)."""
        stmt = select(ComplianceLedgerEntry).order_by(ComplianceLedgerEntry.seq.desc()).limit(1)
        return (await self.session.execute(stmt)).scalars().first()

    async def next_seq(self) -> int:
        """The sequence number the next appended entry should take (1-based)."""
        stmt = select(func.max(ComplianceLedgerEntry.seq))
        current = (await self.session.execute(stmt)).scalar_one_or_none()
        return (int(current) + 1) if current is not None else 1

    async def append(
        self,
        *,
        seq: int,
        category: LedgerCategory,
        event: str,
        entry_hash: str,
        prev_hash: str | None,
        subject_id: str | None = None,
        actor_id: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> ComplianceLedgerEntry:
        """Insert one immutable ledger entry."""
        entry = ComplianceLedgerEntry(
            id=new_id(),
            seq=seq,
            category=category,
            event=event,
            subject_id=subject_id,
            actor_id=actor_id,
            payload=payload,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def all_ordered(self) -> list[ComplianceLedgerEntry]:
        """Every entry in sequence order (for chain verification / export)."""
        stmt = select(ComplianceLedgerEntry).order_by(ComplianceLedgerEntry.seq.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def for_subject(self, subject_id: str) -> list[ComplianceLedgerEntry]:
        """Every ledger entry concerning a subject, in sequence order."""
        stmt = (
            select(ComplianceLedgerEntry)
            .where(ComplianceLedgerEntry.subject_id == subject_id)
            .order_by(ComplianceLedgerEntry.seq.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def by_category(
        self, category: LedgerCategory, *, since: datetime | None = None
    ) -> list[ComplianceLedgerEntry]:
        """Entries in one category (optionally since a time), in sequence order."""
        stmt = select(ComplianceLedgerEntry).where(ComplianceLedgerEntry.category == category)
        if since is not None:
            stmt = stmt.where(ComplianceLedgerEntry.created_at >= since)
        stmt = stmt.order_by(ComplianceLedgerEntry.seq.asc())
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["ComplianceLedgerRepo"]
