"""Repository for the append-only budget ledger (kinora.md §11.1).

The repo owns persistence and the windowed-sum queries; the *policy* (caps,
:class:`BudgetExceeded`) lives in :class:`app.memory.budget_service.BudgetService`.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, select, text

from app.db.base import new_id
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.repositories.base import BaseRepository


def _scope_clauses(
    *, book_id: str | None, session_id: str | None, scene_id: str | None
) -> list[ColumnElement[bool]]:
    """Equality predicates for the non-null scopes (global => no predicates)."""
    clauses: list[ColumnElement[bool]] = []
    if book_id is not None:
        clauses.append(BudgetLedger.book_id == book_id)
    if session_id is not None:
        clauses.append(BudgetLedger.session_id == session_id)
    if scene_id is not None:
        clauses.append(BudgetLedger.scene_id == scene_id)
    return clauses


class BudgetRepo(BaseRepository):
    """Append entries and sum committed / outstanding-reserved seconds."""

    async def append(
        self,
        *,
        kind: BudgetKind,
        video_seconds: float,
        reservation_id: str,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        note: str | None = None,
        entry_id: str | None = None,
    ) -> BudgetLedger:
        """Append one immutable ledger row."""
        entry = BudgetLedger(
            id=entry_id or new_id(),
            kind=kind,
            video_seconds=video_seconds,
            reservation_id=reservation_id,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
            note=note,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def committed_seconds(
        self,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> float:
        """Σ of committed video-seconds within the given scope."""
        stmt = select(func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0)).where(
            BudgetLedger.kind == BudgetKind.COMMIT,
            *_scope_clauses(book_id=book_id, session_id=session_id, scene_id=scene_id),
        )
        return float((await self.session.execute(stmt)).scalar_one())

    async def outstanding_reserved_seconds(
        self,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> float:
        """Σ of reserved seconds for reservations not yet committed or released."""
        closed = select(BudgetLedger.reservation_id).where(
            BudgetLedger.kind.in_((BudgetKind.COMMIT, BudgetKind.RELEASE))
        )
        stmt = select(func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0)).where(
            BudgetLedger.kind == BudgetKind.RESERVE,
            BudgetLedger.reservation_id.not_in(closed),
            *_scope_clauses(book_id=book_id, session_id=session_id, scene_id=scene_id),
        )
        return float((await self.session.execute(stmt)).scalar_one())

    async def used_seconds(
        self,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> float:
        """Committed + outstanding-reserved within the scope (what counts against a cap)."""
        committed = await self.committed_seconds(
            book_id=book_id, session_id=session_id, scene_id=scene_id
        )
        reserved = await self.outstanding_reserved_seconds(
            book_id=book_id, session_id=session_id, scene_id=scene_id
        )
        return committed + reserved

    async def advisory_lock(self, key: int) -> None:
        """Take a transaction-scoped advisory lock to serialize concurrent reserves.

        The lock is released automatically when the surrounding transaction ends
        (commit/rollback in :func:`app.db.session.get_session`), so concurrent
        reservations against the same budget compute their sums one-at-a-time and
        cannot both slip past the ceiling.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=key)
        )
