"""Repository for the Phase-A ingest checkpoint ledger (§9.1, additive).

[Agent: ingest-domain — new repository file, documented in
``app/ingest/DESIGN.md``.]

Records and queries completed ingest milestones so a resumed ingest can skip the
stages it already finished. Idempotent: recording a milestone that is already
present is a no-op (the row's payload is refreshed). All queries no-op-degrade
to "nothing recorded" if the table does not exist yet (an unmigrated deploy),
which the caller (:mod:`app.ingest.checkpoints`) handles.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.ingest_checkpoint import IngestCheckpoint, IngestMilestone
from app.db.repositories.base import BaseRepository


class IngestCheckpointRepo(BaseRepository):
    """Create + query the per-book ingest milestone ledger."""

    async def record(
        self, book_id: str, milestone: IngestMilestone, *, payload: dict[str, Any] | None = None
    ) -> None:
        """Mark ``milestone`` complete for ``book_id`` (idempotent upsert)."""
        stmt = (
            pg_insert(IngestCheckpoint)
            .values(book_id=book_id, milestone=milestone, payload=payload or {})
            .on_conflict_do_update(
                constraint="uq_ingest_checkpoint",
                set_={"payload": payload or {}},
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def completed(self, book_id: str) -> set[IngestMilestone]:
        """Return the set of milestones already completed for ``book_id``."""
        stmt = select(IngestCheckpoint.milestone).where(IngestCheckpoint.book_id == book_id)
        rows = await self.session.execute(stmt)
        return set(rows.scalars().all())

    async def has(self, book_id: str, milestone: IngestMilestone) -> bool:
        """Whether ``milestone`` is already recorded complete for ``book_id``."""
        stmt = select(IngestCheckpoint.id).where(
            IngestCheckpoint.book_id == book_id,
            IngestCheckpoint.milestone == milestone,
        )
        row = await self.session.execute(stmt)
        return row.scalar_one_or_none() is not None

    async def clear(self, book_id: str) -> int:
        """Delete all checkpoints for ``book_id`` (force a full re-ingest)."""
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                delete(IngestCheckpoint).where(IngestCheckpoint.book_id == book_id)
            ),
        )
        await self.session.flush()
        return int(result.rowcount or 0)


__all__ = ["IngestCheckpointRepo"]
