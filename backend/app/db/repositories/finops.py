"""Repository for the append-only USD ``cost_ledger`` (kinora.md §11.1, §12.5).

Persistence + the windowed-sum / group-by queries FinOps reads. The *policy*
(prices, attribution, reconciliation tolerances) lives in :mod:`app.finops`.

USD is stored as integer **micro-dollars** to avoid float drift; this repo
converts a :class:`~decimal.Decimal` USD amount to/from micros on the way in/out.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ColumnElement, func, select

from app.db.base import new_id
from app.db.models.finops import CostKind, CostLedger
from app.db.repositories.base import BaseRepository

_MICROS = Decimal(1_000_000)


def usd_to_micros(usd: Decimal) -> int:
    """Convert a Decimal USD amount to integer micro-dollars (rounded)."""
    return int((usd * _MICROS).to_integral_value())


def micros_to_usd(micros: int) -> Decimal:
    """Convert integer micro-dollars back to a Decimal USD amount."""
    return Decimal(micros) / _MICROS


def _scope_clauses(
    *,
    tenant_id: str | None,
    book_id: str | None,
    session_id: str | None,
    scene_id: str | None,
    shot_id: str | None,
    agent: str | None,
) -> list[ColumnElement[bool]]:
    """Equality predicates for the supplied (non-None) scopes."""
    clauses: list[ColumnElement[bool]] = []
    if tenant_id is not None:
        clauses.append(CostLedger.tenant_id == tenant_id)
    if book_id is not None:
        clauses.append(CostLedger.book_id == book_id)
    if session_id is not None:
        clauses.append(CostLedger.session_id == session_id)
    if scene_id is not None:
        clauses.append(CostLedger.scene_id == scene_id)
    if shot_id is not None:
        clauses.append(CostLedger.shot_id == shot_id)
    if agent is not None:
        clauses.append(CostLedger.agent == agent)
    return clauses


class CostLedgerRepo(BaseRepository):
    """Append USD-valued spend and aggregate it by scope / agent / kind."""

    async def append(
        self,
        *,
        kind: CostKind,
        cost_usd: Decimal,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        images: int = 0,
        audio_seconds: float = 0.0,
        video_seconds: float = 0.0,
        note: str | None = None,
        entry_id: str | None = None,
    ) -> CostLedger:
        """Append one immutable cost-ledger row."""
        entry = CostLedger(
            id=entry_id or new_id(),
            kind=kind,
            cost_micros=usd_to_micros(cost_usd),
            tenant_id=tenant_id,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
            shot_id=shot_id,
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            images=images,
            audio_seconds=audio_seconds,
            video_seconds=video_seconds,
            note=note,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def total_micros(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        agent: str | None = None,
    ) -> int:
        """Σ micro-USD within the supplied scope (0 when empty)."""
        stmt = select(func.coalesce(func.sum(CostLedger.cost_micros), 0)).where(
            *_scope_clauses(
                tenant_id=tenant_id,
                book_id=book_id,
                session_id=session_id,
                scene_id=scene_id,
                shot_id=shot_id,
                agent=agent,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def total_usd(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        agent: str | None = None,
    ) -> Decimal:
        """Σ USD within the supplied scope (0 when empty)."""
        return micros_to_usd(
            await self.total_micros(
                tenant_id=tenant_id,
                book_id=book_id,
                session_id=session_id,
                scene_id=scene_id,
                shot_id=shot_id,
                agent=agent,
            )
        )

    async def video_seconds_total(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
    ) -> float:
        """Σ video-seconds recorded in the cost ledger within scope.

        This is the *cost ledger's* view of video-seconds; reconciliation
        (:mod:`app.finops.ledger`) compares it against the authoritative
        ``budget_ledger`` committed total.
        """
        stmt = select(func.coalesce(func.sum(CostLedger.video_seconds), 0.0)).where(
            *_scope_clauses(
                tenant_id=tenant_id,
                book_id=book_id,
                session_id=session_id,
                scene_id=scene_id,
                shot_id=shot_id,
                agent=None,
            )
        )
        return float((await self.session.execute(stmt)).scalar_one())

    async def by_agent_micros(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, int]:
        """micro-USD grouped by attributed agent within scope."""
        stmt = (
            select(CostLedger.agent, func.coalesce(func.sum(CostLedger.cost_micros), 0))
            .where(
                *_scope_clauses(
                    tenant_id=tenant_id,
                    book_id=book_id,
                    session_id=session_id,
                    scene_id=None,
                    shot_id=None,
                    agent=None,
                )
            )
            .group_by(CostLedger.agent)
        )
        rows = (await self.session.execute(stmt)).all()
        return {(agent or "unknown"): int(micros) for agent, micros in rows}

    async def by_kind_micros(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, int]:
        """micro-USD grouped by spend kind within scope."""
        stmt = (
            select(CostLedger.kind, func.coalesce(func.sum(CostLedger.cost_micros), 0))
            .where(
                *_scope_clauses(
                    tenant_id=tenant_id,
                    book_id=book_id,
                    session_id=session_id,
                    scene_id=None,
                    shot_id=None,
                    agent=None,
                )
            )
            .group_by(CostLedger.kind)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(kind.value if hasattr(kind, "value") else kind): int(m) for kind, m in rows}


__all__ = ["CostLedgerRepo", "micros_to_usd", "usd_to_micros"]
