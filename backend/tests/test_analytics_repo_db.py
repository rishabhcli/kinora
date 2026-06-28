"""Postgres-backed analytics repo + store integration tests.

Run against a throwaway Postgres and SKIP cleanly when
``KINORA_TEST_DATABASE_URL`` is unset (isolated DB ``kinora_analytics_test`` on
:5433). Each test isolates by rolling back on teardown — the repo only flushes,
never commits.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.analytics.events import EventName, ReadMode, TrackedEvent
from app.analytics.rollup import compute_rollups
from app.analytics.sessionize import sessionize
from app.analytics.timebucket import Granularity
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.repositories.analytics import AnalyticsRepo

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping analytics DB tests"
)

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def ev(
    eid: str,
    *,
    minute: float = 0,
    name: EventName = EventName.PAGE_VIEWED,
    user: str | None = "u1",
    book: str | None = "b1",
    session: str | None = None,
    mode: ReadMode | None = None,
    props: dict | None = None,
) -> TrackedEvent:
    return TrackedEvent(
        event_id=eid,
        name=name,
        occurred_at=BASE + timedelta(minutes=minute),
        anon_user_id=user,
        book_id=book,
        session_key=session,
        mode=mode,
        props=props or {},
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


async def test_append_events_idempotent(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    assert await repo.append_events([ev("a"), ev("b")]) == 2
    # re-insert same ids + a new one -> only the new one counts
    assert await repo.append_events([ev("a"), ev("c")]) == 1
    assert await repo.count_events() == 3


async def test_append_within_batch_dedup(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    assert await repo.append_events([ev("a"), ev("a")]) == 1


async def test_query_events_roundtrip_and_filters(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    await repo.append_events(
        [
            ev("a", minute=0, book="b1", user="u1", name=EventName.PAGE_VIEWED),
            ev("b", minute=1, book="b2", user="u2", name=EventName.SEEK, mode=ReadMode.DIRECTOR),
            ev("c", minute=2, book="b1", user="u1", props={"page": 7}),
        ]
    )
    by_book = await repo.query_events(book_id="b1")
    assert [e.event_id for e in by_book] == ["a", "c"]
    # props + mode survive the round-trip
    director = await repo.query_events(names=[EventName.SEEK])
    assert director[0].mode is ReadMode.DIRECTOR
    paged = await repo.query_events(book_id="b1")
    assert paged[1].prop_int("page") == 7


async def test_query_time_window_half_open(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    await repo.append_events([ev("a", minute=0), ev("b", minute=10), ev("c", minute=20)])
    rows = await repo.query_events(
        since=BASE + timedelta(minutes=10), until=BASE + timedelta(minutes=20)
    )
    assert [e.event_id for e in rows] == ["b"]


async def test_upsert_sessions_idempotent(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    events = [
        ev("a", minute=0, props={"page": 0, "page_count": 4}),
        ev("b", minute=5, props={"page": 3, "page_count": 4}),
    ]
    sessions = sessionize(events)
    assert await repo.upsert_sessions(sessions) == len(sessions)
    # upsert again -> no duplicate rows (same session_id)
    await repo.upsert_sessions(sessions)
    count_q = await session.execute(text("SELECT count(*) FROM analytics_sessions"))
    assert int(count_q.scalar_one()) == len(sessions)
    ratio_q = await session.execute(text("SELECT completion_ratio FROM analytics_sessions"))
    assert float(ratio_q.scalar_one()) == 1.0


async def test_upsert_rollups_idempotent_and_updates_value(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    events = [ev("a", minute=0, user="u1"), ev("b", minute=1, user="u2")]
    rows = compute_rollups(events, granularity=Granularity.DAY)
    await repo.upsert_rollups(rows)
    first_count = (
        await session.execute(text("SELECT count(*) FROM analytics_daily_rollup"))
    ).scalar_one()
    # recompute with an extra event -> active_users for the bucket updates in place
    events.append(ev("c", minute=2, user="u3"))
    rows2 = compute_rollups(events, granularity=Granularity.DAY)
    await repo.upsert_rollups(rows2)
    second_count = (
        await session.execute(text("SELECT count(*) FROM analytics_daily_rollup"))
    ).scalar_one()
    assert int(second_count) == int(first_count)  # no duplicate grain rows
    au = (
        await session.execute(
            text("SELECT value FROM analytics_daily_rollup WHERE metric='active_users'")
        )
    ).scalar_one()
    assert float(au) == 3.0


async def test_read_rollups_filtered(session: AsyncSession) -> None:
    repo = AnalyticsRepo(session)
    events = [ev("a", minute=0, user="u1")]
    await repo.upsert_rollups(compute_rollups(events, granularity=Granularity.DAY))
    read = await repo.read_rollups(metric="active_users", granularity="day")
    assert read and all(r.metric == "active_users" for r in read)


async def test_postgres_store_via_session_factory() -> None:
    """The PostgresAnalyticsStore satisfies the AnalyticsStore protocol over a factory."""
    from contextlib import asynccontextmanager

    from app.analytics.store import AnalyticsStore
    from app.analytics.store_pg import PostgresAnalyticsStore

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    store = PostgresAnalyticsStore(factory)
    assert isinstance(store, AnalyticsStore)
    try:
        eid = "pg-store-1"
        assert await store.append([ev(eid)]) == 1
        assert await store.append([ev(eid)]) == 0  # idempotent
        rows = await store.query(book_id="b1")
        assert any(r.event_id == eid for r in rows)
    finally:
        # clean up the committed rows this test wrote
        async with maker() as db:
            await db.execute(text("DELETE FROM analytics_events WHERE event_id LIKE 'pg-store-%'"))
            await db.commit()
        await engine.dispose()


async def test_postgres_summary_sink_persists(session: AsyncSession) -> None:
    """The PostgresSummarySink upserts rollups + sessions through the repo."""
    from contextlib import asynccontextmanager

    from app.analytics.sink import SummarySink
    from app.analytics.sink_pg import PostgresSummarySink

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        # Reuse the test's rolled-back session so writes are cleaned up.
        yield session

    sink = PostgresSummarySink(factory)
    assert isinstance(sink, SummarySink)

    events = [
        ev("a", minute=0, user="u1", props={"page": 0, "page_count": 4}),
        ev("b", minute=5, user="u1", props={"page": 3, "page_count": 4}),
    ]
    rollups = compute_rollups(events, granularity=Granularity.DAY)
    assert await sink.write_rollups(rollups) == len(rollups)
    assert await sink.write_sessions(sessionize(events)) == 1

    # rows landed in the tables (visible within the same uncommitted transaction)
    n_roll = (
        await session.execute(text("SELECT count(*) FROM analytics_daily_rollup"))
    ).scalar_one()
    n_sess = (await session.execute(text("SELECT count(*) FROM analytics_sessions"))).scalar_one()
    assert int(n_roll) == len(rollups)
    assert int(n_sess) == 1
