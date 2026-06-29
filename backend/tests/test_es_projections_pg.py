"""DB-backed projection store integration tests.

Exercises the Postgres-backed :class:`ReadModelStore` / :class:`CheckpointStore`
/ :class:`SlotDirectory` against the ``esproj_*`` tables, plus an end-to-end
catch-up + blue-green rebuild over Postgres. SKIPS cleanly when
``KINORA_TEST_DATABASE_URL`` is unset (the in-memory suites cover the logic);
the autouse conftest isolation TRUNCATES every table (incl. ``esproj_*``) before
each test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register esproj_* tables on Base.metadata)
from app.eventsourcing.projections.bluegreen import (
    BlueGreenRebuilder,
    Slot,
    slot_namespace,
)
from app.eventsourcing.projections.checkpoints import ProjectionStatus
from app.eventsourcing.projections.examples.session_timeline import (
    SessionTimelineProjection,
)
from app.eventsourcing.projections.lag import ConsistencyToken, LagTracker
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.runtime import ProjectionRuntime
from app.eventsourcing.projections.stores_pg import (
    PostgresCheckpointStore,
    PostgresReadModelStore,
)

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
    ),
]


@pytest_asyncio.fixture
async def session_factory():  # type: ignore[no-untyped-def]
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield factory
    finally:
        await engine.dispose()


async def test_pg_read_model_store_crud(session_factory) -> None:  # type: ignore[no-untyped-def]
    store = PostgresReadModelStore(session_factory)
    r1 = await store.put("ns", "k", {"x": 1})
    assert r1.version == 1
    r2 = await store.put("ns", "k", {"x": 2})
    assert r2.version == 2
    got = await store.get("ns", "k")
    assert got is not None and got.value == {"x": 2}
    await store.put("ns", "a:1", {})
    await store.put("ns", "a:2", {})
    rows = await store.list("ns", prefix="a:")
    assert [r.key for r in rows] == ["a:1", "a:2"]
    assert await store.count("ns") == 3
    assert await store.delete("ns", "k") is True
    assert await store.clear("ns") == 2


async def test_pg_checkpoint_store_forward_only_and_dedupe(session_factory) -> None:  # type: ignore[no-untyped-def]
    cps = PostgresCheckpointStore(session_factory)
    await cps.advance("p", 5, status=ProjectionStatus.CATCHING_UP)
    await cps.advance("p", 2)  # backwards: no-op
    cp = await cps.load("p")
    assert cp.position == 5
    # Applied-event dedupe via the unique constraint.
    assert await cps.mark_applied("p", "e1") is True
    assert await cps.mark_applied("p", "e1") is False  # re-delivery
    assert await cps.was_applied("p", "e1") is True
    # Reset clears position + the applied ledger.
    await cps.reset("p")
    assert (await cps.load("p")).position == 0
    assert await cps.was_applied("p", "e1") is False


async def test_pg_end_to_end_catch_up(session_factory) -> None:  # type: ignore[no-untyped-def]
    es = InMemoryEventStore()
    rms = PostgresReadModelStore(session_factory)
    cps = PostgresCheckpointStore(session_factory)
    sid = "session:s1"
    await es.append(sid, "session.started", {"book_id": "b1"})
    await es.append(sid, "session.page_viewed", {"page": 5})
    await es.append(sid, "session.ended", {"duration_s": 10.0})
    rt = ProjectionRuntime(
        SessionTimelineProjection(), event_store=es, read_models=rms, checkpoints=cps
    )
    result = await rt.catch_up()
    assert result.applied == 3
    row = await rms.get("session_timeline", sid)
    assert row is not None
    assert row.value["status"] == "ended"
    assert row.value["deepest_page"] == 5

    # Read-your-writes: the projection has consumed the head position.
    tracker = LagTracker(event_store=es, checkpoints=cps)
    head = await es.head_position()
    assert await tracker.has_caught_up(
        ConsistencyToken(position=head, projection="session_timeline")
    )


async def test_pg_blue_green_rebuild(session_factory) -> None:  # type: ignore[no-untyped-def]
    es = InMemoryEventStore()
    rms = PostgresReadModelStore(session_factory)
    cps = PostgresCheckpointStore(session_factory)  # doubles as the slot directory
    await es.append("session:s1", "session.started", {"book_id": "b1"})
    await es.append("session:s1", "session.ended", {})
    rebuilder = BlueGreenRebuilder(
        event_store=es, read_models=rms, checkpoints=cps, directory=cps
    )
    report = await rebuilder.rebuild(SessionTimelineProjection())
    assert report.to_slot == Slot.GREEN
    active = await rebuilder.active_namespace("session_timeline")
    assert active == slot_namespace("session_timeline", Slot.GREEN)
    row = await rms.get(active, "session:s1")
    assert row is not None and row.value["status"] == "ended"
