"""Postgres-store integration tests — the behaviours that *need* a real engine.

Skipped cleanly unless ``ES_STORE_TEST`` (or ``KINORA_TEST_DATABASE_URL``) points
at a throwaway database. They verify the properties the in-memory store cannot
exercise:

* the global sequence stays **gap-free across a rolled-back append**,
* the unique ``(stream_id, version)`` constraint is the **hard OCC backstop** when
  two committed transactions both read the same current version,
* the inbox ``ON CONFLICT DO NOTHING`` race resolves to one winner,
* the relay's ``FOR UPDATE SKIP LOCKED`` lets two concurrent claimers split a
  backlog without overlap.

Each test runs against the isolated ``es_*`` tables, truncated up front.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.eventsourcing.store import (
    NO_STREAM,
    EventData,
    OptimisticConcurrencyError,
)
from app.eventsourcing.store.inbox import PostgresInboxRepository
from app.eventsourcing.store.models import EventStoreEvent
from app.eventsourcing.store.postgres import PostgresEventStore

_ES_DB_URL = os.environ.get("ES_STORE_TEST") or os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _ES_DB_URL,
    reason="Postgres event-store tests require ES_STORE_TEST or KINORA_TEST_DATABASE_URL",
)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker]:
    from app.db import models  # noqa: F401
    from app.db.base import Base

    assert _ES_DB_URL
    engine = create_async_engine(_ES_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "TRUNCATE es_events, es_snapshots, es_outbox, es_inbox, es_sequence, "
                "es_checkpoints RESTART IDENTITY CASCADE"
            )
        )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _ev(t: str) -> EventData:
    return EventData(event_type=t, payload={"t": t})


@pytest.mark.asyncio
async def test_sequence_is_gap_free_after_rollback(maker: async_sessionmaker) -> None:
    # Append #1 commits → positions 1,2.
    async with maker() as s:
        store = PostgresEventStore(s)
        recs = await store.append("a", [_ev("a0"), _ev("a1")], expected_version=NO_STREAM)
        await s.commit()
    assert [r.global_position for r in recs] == [1, 2]

    # A transaction that allocates positions 3,4 but then rolls back must NOT
    # burn those numbers (the whole point of the counter-row allocator).
    async with maker() as s:
        store = PostgresEventStore(s)
        await store.append("b", [_ev("b0"), _ev("b1")], expected_version=NO_STREAM)
        await s.rollback()

    # The next committed append reuses 3,4 — gap-free.
    async with maker() as s:
        store = PostgresEventStore(s)
        recs2 = await store.append("c", [_ev("c0"), _ev("c1")], expected_version=NO_STREAM)
        await s.commit()
    assert [r.global_position for r in recs2] == [3, 4]

    # And the on-disk log is exactly 1..4 with no holes.
    async with maker() as s:
        positions = (
            await s.execute(select(EventStoreEvent.global_position).order_by("global_position"))
        ).scalars().all()
    assert positions == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_unique_constraint_is_the_occ_backstop(maker: async_sessionmaker) -> None:
    # Seed a stream at version 0.
    async with maker() as s:
        await PostgresEventStore(s).append("s", [_ev("seed")], expected_version=NO_STREAM)
        await s.commit()

    # Two sessions both read current version 0 and try to append version 1.
    s1 = maker()
    s2 = maker()
    try:
        store1 = PostgresEventStore(s1)
        store2 = PostgresEventStore(s2)
        # Both pass the in-Python check (each sees current version 0)…
        await store1.append("s", [_ev("w1")], expected_version=0)
        await s1.commit()  # first writer wins
        # …but the second one trips the unique (stream_id, version) backstop and
        # is surfaced as a concurrency conflict (not a raw IntegrityError).
        with pytest.raises(OptimisticConcurrencyError):
            await store2.append("s", [_ev("w2")], expected_version=0)
            await s2.commit()
    finally:
        await s1.close()
        await s2.rollback()
        await s2.close()

    async with maker() as s:
        version = await PostgresEventStore(s).stream_version("s")
    assert version == 1  # exactly one writer appended


@pytest.mark.asyncio
async def test_inbox_on_conflict_resolves_one_winner(maker: async_sessionmaker) -> None:
    async with maker() as s:
        inbox = PostgresInboxRepository(s)
        assert not await inbox.already_processed("proj", "m1")
        assert await inbox.mark_processed("proj", "m1", result={"ok": 1})
        await s.commit()

    # A redelivery in a fresh transaction: the insert conflicts → returns False.
    async with maker() as s:
        inbox = PostgresInboxRepository(s)
        assert await inbox.already_processed("proj", "m1")
        assert not await inbox.mark_processed("proj", "m1")
        await s.commit()


@pytest.mark.asyncio
async def test_postgres_checkpoint_durably_resumes(maker: async_sessionmaker) -> None:
    from app.eventsourcing.store import CatchUpSubscription, RecordedEvent
    from app.eventsourcing.store.checkpoint import PostgresCheckpointStore

    async with maker() as s:
        await PostgresEventStore(s).append(
            "s", [_ev(f"e{i}") for i in range(5)], expected_version=NO_STREAM
        )
        await s.commit()

    seen: list[int] = []

    async def handler(event: RecordedEvent) -> None:
        seen.append(event.global_position)

    # First pass processes 3 events and commits the checkpoint.
    async with maker() as s:
        sub = CatchUpSubscription(
            "proj", PostgresEventStore(s), PostgresCheckpointStore(s), handler, batch_size=3
        )
        r1 = await sub.run_once()
        await s.commit()
    assert r1.processed == 3
    assert seen == [1, 2, 3]

    # A fresh session resumes from the durable checkpoint (no reprocessing).
    seen.clear()
    async with maker() as s:
        sub = CatchUpSubscription(
            "proj", PostgresEventStore(s), PostgresCheckpointStore(s), handler, batch_size=10
        )
        r2 = await sub.run_until_caught_up()
        await s.commit()
    assert seen == [4, 5]
    assert r2.caught_up

    async with maker() as s:
        cp = await PostgresCheckpointStore(s).load("proj")
    assert cp.position == 5
    assert cp.events_processed == 5


@pytest.mark.asyncio
async def test_relay_claim_skips_locked_rows(maker: async_sessionmaker) -> None:
    from app.eventsourcing.store.postgres import PostgresOutboxRepository

    # Enqueue 4 published events.
    async with maker() as s:
        store = PostgresEventStore(s)
        await store.append(
            "s", [_ev(f"e{i}") for i in range(4)], expected_version=NO_STREAM, publish_topic="t"
        )
        await s.commit()

    # Worker 1 opens a transaction and claims 2 rows (locks them).
    s1 = maker()
    s2 = maker()
    try:
        repo1 = PostgresOutboxRepository(s1)
        repo2 = PostgresOutboxRepository(s2)
        claimed1 = await repo1.claim_batch(limit=2)
        # Worker 2, concurrently, claims the next 2 — SKIP LOCKED means it never
        # sees worker 1's locked rows.
        claimed2 = await repo2.claim_batch(limit=10)
        ids1 = {r.id for r in claimed1}
        ids2 = {r.id for r in claimed2}
        assert len(claimed1) == 2
        assert ids1.isdisjoint(ids2)  # no overlap
        assert len(claimed2) == 2  # the remaining two
        await s1.commit()
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
