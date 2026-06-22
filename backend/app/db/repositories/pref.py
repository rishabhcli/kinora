"""Repository for director-preference signals (kinora.md §8.6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import InstrumentedAttribute

from app.db.base import new_id
from app.db.models.pref import Pref
from app.db.repositories.base import BaseRepository


def _scope(column: InstrumentedAttribute[Any], value: str | None) -> ColumnElement[bool]:
    """Equality predicate that uses ``IS NULL`` when ``value`` is ``None``."""
    return column.is_(None) if value is None else column == value


class PrefsRepo(BaseRepository):
    """Read and nudge accumulated preferences, scoped by user and/or book."""

    async def get(
        self,
        *,
        user_id: str | None = None,
        book_id: str | None = None,
        kind: str | None = None,
    ) -> list[Pref]:
        """Return preferences filtered by any combination of user, book, kind."""
        stmt = select(Pref)
        if user_id is not None:
            stmt = stmt.where(Pref.user_id == user_id)
        if book_id is not None:
            stmt = stmt.where(Pref.book_id == book_id)
        if kind is not None:
            stmt = stmt.where(Pref.kind == kind)
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert_nudge(
        self,
        *,
        kind: str,
        value: dict[str, Any],
        user_id: str | None = None,
        book_id: str | None = None,
        weight_delta: float = 1.0,
    ) -> Pref:
        """Create or reinforce a preference for the exact ``(user, book, kind)`` scope.

        A repeated edit *nudges* the existing signal: its value is replaced and
        its weight accumulates by ``weight_delta``.
        """
        stmt = select(Pref).where(
            Pref.kind == kind,
            _scope(Pref.user_id, user_id),
            _scope(Pref.book_id, book_id),
        )
        existing = (await self.session.execute(stmt)).scalars().first()
        if existing is None:
            pref = Pref(
                id=new_id(),
                user_id=user_id,
                book_id=book_id,
                kind=kind,
                value=value,
                weight=weight_delta,
            )
            self.session.add(pref)
        else:
            existing.value = value
            existing.weight = existing.weight + weight_delta
            pref = existing
        await self.session.flush()
        return pref
