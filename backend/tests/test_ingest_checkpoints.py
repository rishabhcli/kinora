"""Ingest checkpoint ledger tests (§9.1 resumable milestones) — DB-backed.

SKIPs when ``KINORA_TEST_DATABASE_URL`` is unset. Covers the repo upsert
semantics and the fault-tolerant :mod:`app.ingest.checkpoints` façade (including
graceful degradation when the table is absent).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ingest_checkpoint import IngestMilestone
from app.db.repositories.book import BookRepo
from app.db.repositories.ingest_checkpoint import IngestCheckpointRepo
from app.ingest.checkpoints import (
    clear_checkpoints,
    completed_milestones,
    record_milestone,
)
from tests.test_ingest_support import (
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

pytestmark = requires_db


async def test_record_and_query_milestones(session: AsyncSession) -> None:  # noqa: F811
    book = await BookRepo(session).create(title="Checkpointed")
    repo = IngestCheckpointRepo(session)

    assert await repo.completed(book.id) == set()

    await repo.record(book.id, IngestMilestone.EXTRACT, payload={"num_pages": 12})
    await repo.record(book.id, IngestMilestone.ANALYZE)

    done = await repo.completed(book.id)
    assert done == {IngestMilestone.EXTRACT, IngestMilestone.ANALYZE}
    assert await repo.has(book.id, IngestMilestone.EXTRACT) is True
    assert await repo.has(book.id, IngestMilestone.CANON) is False


async def test_record_is_idempotent_upsert(session: AsyncSession) -> None:  # noqa: F811
    book = await BookRepo(session).create(title="Idempotent")
    repo = IngestCheckpointRepo(session)

    await repo.record(book.id, IngestMilestone.EXTRACT, payload={"v": 1})
    await repo.record(book.id, IngestMilestone.EXTRACT, payload={"v": 2})  # no duplicate

    rows = (
        await session.execute(
            text("SELECT payload FROM ingest_checkpoints WHERE book_id = :b"),
            {"b": book.id},
        )
    ).all()
    assert len(rows) == 1
    assert rows[0][0] == {"v": 2}  # payload refreshed on conflict


async def test_clear_removes_all(session: AsyncSession) -> None:  # noqa: F811
    book = await BookRepo(session).create(title="Cleared")
    repo = IngestCheckpointRepo(session)
    await repo.record(book.id, IngestMilestone.EXTRACT)
    await repo.record(book.id, IngestMilestone.ANALYZE)

    removed = await repo.clear(book.id)
    assert removed == 2
    assert await repo.completed(book.id) == set()


async def test_facade_round_trip(session: AsyncSession) -> None:  # noqa: F811
    book = await BookRepo(session).create(title="Facade")

    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        yield session

    assert await completed_milestones(factory, book.id) == set()
    await record_milestone(factory, book.id, IngestMilestone.SHOT_PLAN, payload={"shots": 3})
    assert IngestMilestone.SHOT_PLAN in await completed_milestones(factory, book.id)
    await clear_checkpoints(factory, book.id)
    assert await completed_milestones(factory, book.id) == set()


async def test_facade_degrades_on_db_error() -> None:
    """A DB error (e.g. the table is absent) degrades to empty / no-op, not a raise."""
    from sqlalchemy.exc import ProgrammingError

    class _BrokenSession:
        async def execute(self, *a: object, **k: object) -> None:
            raise ProgrammingError("SELECT ...", {}, Exception("relation does not exist"))

        async def flush(self) -> None:  # pragma: no cover - never reached
            pass

    @asynccontextmanager
    async def factory():  # type: ignore[no-untyped-def]
        yield _BrokenSession()

    # Reads degrade to empty; writes/clears no-op — none of these raise.
    assert await completed_milestones(factory, "book_x") == set()
    await record_milestone(factory, "book_x", IngestMilestone.EXTRACT)
    await clear_checkpoints(factory, "book_x")
