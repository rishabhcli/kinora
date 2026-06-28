"""Repository for legal holds."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.compliance.db.models import LegalHold
from app.compliance.enums import DataClass, HoldStatus
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class LegalHoldRepo(BaseRepository):
    """Place, lift and query legal holds."""

    async def place(
        self,
        *,
        subject_id: str,
        matter_id: str,
        reason: str,
        placed_at: datetime,
        data_class: DataClass | None = None,
        placed_by: str = "system",
        hold_id: str | None = None,
    ) -> LegalHold:
        """Insert an active hold over a subject (optionally one data class)."""
        hold = LegalHold(
            id=hold_id or new_id(),
            subject_id=subject_id,
            data_class=data_class,
            status=HoldStatus.ACTIVE,
            matter_id=matter_id,
            reason=reason,
            placed_by=placed_by,
            placed_at=placed_at,
        )
        self.session.add(hold)
        await self.session.flush()
        return hold

    async def get(self, hold_id: str) -> LegalHold | None:
        """Fetch a hold by id."""
        return await self.session.get(LegalHold, hold_id)

    async def lift(self, hold_id: str, *, lifted_by: str, lifted_at: datetime) -> LegalHold | None:
        """Mark an active hold as lifted; returns the updated hold (or None)."""
        hold = await self.get(hold_id)
        if hold is None:
            return None
        hold.status = HoldStatus.LIFTED
        hold.lifted_by = lifted_by
        hold.lifted_at = lifted_at
        await self.session.flush()
        return hold

    async def active_for_subject(self, subject_id: str) -> list[LegalHold]:
        """Every active hold covering a subject."""
        stmt = select(LegalHold).where(
            LegalHold.subject_id == subject_id,
            LegalHold.status == HoldStatus.ACTIVE,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_active(self) -> list[LegalHold]:
        """Every active hold (the legal-hold register)."""
        stmt = select(LegalHold).where(LegalHold.status == HoldStatus.ACTIVE)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["LegalHoldRepo"]
