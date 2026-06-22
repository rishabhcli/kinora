"""Repository for logged shot defects (kinora.md §9.5, §12.4)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.base import new_id
from app.db.models.defect import Defect
from app.db.repositories.base import BaseRepository


class DefectRepo(BaseRepository):
    """Log and query shot defects."""

    async def log(
        self,
        *,
        book_id: str,
        kind: str,
        shot_id: str | None = None,
        detail: dict[str, Any] | None = None,
        defect_id: str | None = None,
    ) -> Defect:
        """Record a defect (QA failure, degradation-ladder drop, ...)."""
        defect = Defect(
            id=defect_id or new_id(),
            book_id=book_id,
            kind=kind,
            shot_id=shot_id,
            detail=detail,
        )
        self.session.add(defect)
        await self.session.flush()
        return defect

    async def list_for_book(self, book_id: str) -> list[Defect]:
        """Return all defects logged against a book, newest first."""
        stmt = (
            select(Defect).where(Defect.book_id == book_id).order_by(Defect.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
