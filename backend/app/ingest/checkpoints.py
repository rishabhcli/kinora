"""Resumable-milestone helper for Phase-A ingest (§9.1).

Wraps :class:`app.db.repositories.ingest_checkpoint.IngestCheckpointRepo` with
the **fault-tolerant** behaviour the service needs: a checkpoint read/write must
never fail an ingest. If the ``ingest_checkpoints`` table is absent (a deploy
that has not run the migration) or any DB error occurs, the helper degrades to
"nothing recorded" / "best-effort write" so the pipeline simply re-runs every
stage exactly as it did before checkpoints existed.

The service consults :func:`completed_milestones` once at the start of a run and
calls :func:`record_milestone` after each stage it finishes; a resumed ingest
then skips the already-completed prefix. Because every stage is *also* idempotent
on its own (extraction skips existing pages, shot-plan clears-then-inserts), the
checkpoint ledger is an optimisation — correctness never depends on it.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.ingest_checkpoint import IngestMilestone
from app.db.repositories.ingest_checkpoint import IngestCheckpointRepo

logger = get_logger("app.ingest.checkpoints")

SessionFactory = Any  # Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def completed_milestones(
    session_factory: SessionFactory, book_id: str
) -> set[IngestMilestone]:
    """Return the milestones already completed for ``book_id`` (empty on any error)."""
    try:
        ctx: AbstractAsyncContextManager[AsyncSession] = session_factory()
        async with ctx as session:
            return await IngestCheckpointRepo(session).completed(book_id)
    except SQLAlchemyError as exc:
        logger.warning("ingest.checkpoint.read_failed", book_id=book_id, error=str(exc))
        return set()


async def record_milestone(
    session_factory: SessionFactory,
    book_id: str,
    milestone: IngestMilestone,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort record that ``milestone`` finished for ``book_id`` (never raises)."""
    try:
        ctx: AbstractAsyncContextManager[AsyncSession] = session_factory()
        async with ctx as session:
            await IngestCheckpointRepo(session).record(book_id, milestone, payload=payload)
    except SQLAlchemyError as exc:
        logger.warning(
            "ingest.checkpoint.write_failed",
            book_id=book_id,
            milestone=str(milestone),
            error=str(exc),
        )


async def clear_checkpoints(session_factory: SessionFactory, book_id: str) -> None:
    """Best-effort clear of a book's checkpoints (used by ``force`` re-ingest)."""
    try:
        ctx: AbstractAsyncContextManager[AsyncSession] = session_factory()
        async with ctx as session:
            await IngestCheckpointRepo(session).clear(book_id)
    except SQLAlchemyError as exc:
        logger.warning("ingest.checkpoint.clear_failed", book_id=book_id, error=str(exc))


__all__ = ["clear_checkpoints", "completed_milestones", "record_milestone"]
