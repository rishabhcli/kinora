"""Shared behavioural conformance suite for the event store.

Every test here is parametrised over the available store backends so the
in-memory and Postgres implementations are held to the **identical** contract —
ordering (global + per-stream), optimistic concurrency, idempotent re-append,
snapshots, and the transactional outbox. The in-memory backend always runs; the
Postgres backend runs only when ``ES_STORE_TEST`` (or ``KINORA_TEST_DATABASE_URL``)
points at a throwaway database, and is skipped cleanly otherwise.

Because the Postgres store only flushes (the unit of work owns the commit), the
Postgres adapter wraps each operation in its own committing session — which is
exactly how a real caller drives it — so cross-operation reads see prior writes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.eventsourcing.store import (
    ANY,
    NO_EVENTS,
    NO_STREAM,
    STREAM_EXISTS,
    EventData,
    EventMetadata,
    InMemoryEventStore,
    OptimisticConcurrencyError,
    OutboxRecord,
    OutboxStatus,
    RecordedEvent,
    Snapshot,
)
from app.eventsourcing.store.versioning import ExpectedVersion

_ES_DB_URL = os.environ.get("ES_STORE_TEST") or os.environ.get("KINORA_TEST_DATABASE_URL")


# --------------------------------------------------------------------------- #
# A uniform driver over either backend
# --------------------------------------------------------------------------- #


class StoreDriver(Protocol):
    """The operations the conformance suite needs, backend-independent."""

    async def append(
        self,
        stream_id: str,
        events: Sequence[EventData],
        *,
        expected_version: ExpectedVersion,
        publish_topic: str | None = None,
    ) -> tuple[RecordedEvent, ...]: ...

    async def read_stream(
        self, stream_id: str, *, from_version: int = 0, limit: int | None = None
    ) -> tuple[RecordedEvent, ...]: ...

    async def read_all(
        self, *, from_position: int = 0, limit: int = 100
    ) -> tuple[RecordedEvent, ...]: ...

    async def stream_version(self, stream_id: str) -> int: ...

    async def last_position(self) -> int: ...

    async def save_snapshot(self, snapshot: Snapshot) -> None: ...

    async def load_snapshot(self, stream_id: str) -> Snapshot | None: ...

    async def outbox_rows(self) -> list[OutboxRecord]: ...


class MemoryDriver:
    def __init__(self) -> None:
        self.store = InMemoryEventStore()

    async def append(self, stream_id, events, *, expected_version, publish_topic=None):  # type: ignore[no-untyped-def]
        return await self.store.append(
            stream_id, events, expected_version=expected_version, publish_topic=publish_topic
        )

    async def read_stream(self, stream_id, *, from_version=0, limit=None):  # type: ignore[no-untyped-def]
        sl = await self.store.read_stream(stream_id, from_version=from_version, limit=limit)
        return sl.events

    async def read_all(self, *, from_position=0, limit=100):  # type: ignore[no-untyped-def]
        return await self.store.read_all(from_position=from_position, limit=limit)

    async def stream_version(self, stream_id):  # type: ignore[no-untyped-def]
        return await self.store.stream_version(stream_id)

    async def last_position(self):  # type: ignore[no-untyped-def]
        return await self.store.last_position()

    async def save_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        await self.store.save(snapshot)

    async def load_snapshot(self, stream_id):  # type: ignore[no-untyped-def]
        return await self.store.load_latest(stream_id)

    async def outbox_rows(self):  # type: ignore[no-untyped-def]
        return list(self.store.all_outbox())


class PostgresDriver:
    """Drives :class:`PostgresEventStore` with one committing session per op."""

    def __init__(self, maker: async_sessionmaker[Any]) -> None:
        self._maker = maker

    async def _run(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        async with self._maker() as session:
            try:
                result = await fn(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    async def append(self, stream_id, events, *, expected_version, publish_topic=None):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        async def op(session: Any) -> tuple[RecordedEvent, ...]:
            store = PostgresEventStore(session)
            return await store.append(
                stream_id, events, expected_version=expected_version, publish_topic=publish_topic
            )

        return await self._run(op)

    async def read_stream(self, stream_id, *, from_version=0, limit=None):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        async def op(session: Any) -> tuple[RecordedEvent, ...]:
            sl = await PostgresEventStore(session).read_stream(
                stream_id, from_version=from_version, limit=limit
            )
            return sl.events

        return await self._run(op)

    async def read_all(self, *, from_position=0, limit=100):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        async def op(session: Any) -> tuple[RecordedEvent, ...]:
            return await PostgresEventStore(session).read_all(
                from_position=from_position, limit=limit
            )

        return await self._run(op)

    async def stream_version(self, stream_id):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        return await self._run(lambda s: PostgresEventStore(s).stream_version(stream_id))

    async def last_position(self):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        return await self._run(lambda s: PostgresEventStore(s).last_position())

    async def save_snapshot(self, snapshot):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        return await self._run(lambda s: PostgresEventStore(s).save(snapshot))

    async def load_snapshot(self, stream_id):  # type: ignore[no-untyped-def]
        from app.eventsourcing.store import PostgresEventStore

        return await self._run(lambda s: PostgresEventStore(s).load_latest(stream_id))

    async def outbox_rows(self):  # type: ignore[no-untyped-def]
        from sqlalchemy import select

        from app.eventsourcing.store.models import EventStoreOutbox
        from app.eventsourcing.store.postgres import PostgresOutboxRepository  # noqa: F401

        async def op(session: Any) -> list[OutboxRecord]:
            rows = (
                await session.execute(
                    select(EventStoreOutbox).order_by(EventStoreOutbox.global_position)
                )
            ).scalars().all()
            return [
                OutboxRecord(
                    id=r.id,
                    event_id=r.event_id,
                    global_position=r.global_position,
                    topic=r.topic,
                    payload=dict(r.payload),
                    status=OutboxStatus(r.status),
                    attempts=r.attempts,
                    available_at=r.available_at,
                    created_at=r.created_at,
                    published_at=r.published_at,
                    last_error=r.last_error,
                )
                for r in rows
            ]

        return await self._run(op)


# --------------------------------------------------------------------------- #
# Fixtures / parametrisation
# --------------------------------------------------------------------------- #


_PG_MARK = pytest.param(
    "postgres",
    marks=pytest.mark.skipif(
        not _ES_DB_URL,
        reason="Postgres event-store tests require ES_STORE_TEST or KINORA_TEST_DATABASE_URL",
    ),
)


@pytest_asyncio.fixture(params=["memory", _PG_MARK])
async def driver(request: pytest.FixtureRequest) -> AsyncIterator[StoreDriver]:
    if request.param == "memory":
        yield MemoryDriver()
        return

    # Postgres: a clean schema, with the es_* tables truncated for this test.
    from sqlalchemy import text

    from app.db import models  # noqa: F401  (register tables)
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
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield PostgresDriver(maker)
    finally:
        await engine.dispose()


def _ev(t: str, **payload: Any) -> EventData:
    return EventData(event_type=t, payload=payload)


# --------------------------------------------------------------------------- #
# Append + ordering
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_assigns_dense_stream_versions(driver: StoreDriver) -> None:
    recs = await driver.append(
        "s1", [_ev("a"), _ev("b"), _ev("c")], expected_version=NO_STREAM
    )
    assert [r.version for r in recs] == [0, 1, 2]
    assert await driver.stream_version("s1") == 2


@pytest.mark.asyncio
async def test_append_assigns_gap_free_global_positions(driver: StoreDriver) -> None:
    await driver.append("s1", [_ev("a"), _ev("b")], expected_version=NO_STREAM)
    await driver.append("s2", [_ev("c")], expected_version=NO_STREAM)
    await driver.append("s1", [_ev("d")], expected_version=1)

    everything = await driver.read_all(from_position=0, limit=100)
    positions = [r.global_position for r in everything]
    assert positions == [1, 2, 3, 4]  # dense, gap-free, store-wide
    assert await driver.last_position() == 4


@pytest.mark.asyncio
async def test_read_all_is_global_order_across_streams(driver: StoreDriver) -> None:
    await driver.append("a", [_ev("a0")], expected_version=NO_STREAM)
    await driver.append("b", [_ev("b0")], expected_version=NO_STREAM)
    await driver.append("a", [_ev("a1")], expected_version=0)
    out = await driver.read_all()
    assert [(r.stream_id, r.event_type) for r in out] == [
        ("a", "a0"),
        ("b", "b0"),
        ("a", "a1"),
    ]


@pytest.mark.asyncio
async def test_read_all_paging_by_position(driver: StoreDriver) -> None:
    await driver.append("s", [_ev(f"e{i}") for i in range(5)], expected_version=NO_STREAM)
    page1 = await driver.read_all(from_position=0, limit=2)
    assert [r.global_position for r in page1] == [1, 2]
    page2 = await driver.read_all(from_position=2, limit=2)
    assert [r.global_position for r in page2] == [3, 4]
    page3 = await driver.read_all(from_position=4, limit=2)
    assert [r.global_position for r in page3] == [5]


@pytest.mark.asyncio
async def test_read_stream_forward_and_from_version(driver: StoreDriver) -> None:
    await driver.append("s", [_ev(f"e{i}") for i in range(4)], expected_version=NO_STREAM)
    all_events = await driver.read_stream("s")
    assert [e.version for e in all_events] == [0, 1, 2, 3]
    tail = await driver.read_stream("s", from_version=2)
    assert [e.version for e in tail] == [2, 3]
    limited = await driver.read_stream("s", from_version=0, limit=2)
    assert [e.version for e in limited] == [0, 1]


@pytest.mark.asyncio
async def test_empty_stream_version_and_read(driver: StoreDriver) -> None:
    assert await driver.stream_version("nope") == NO_EVENTS
    assert await driver.read_stream("nope") == ()


# --------------------------------------------------------------------------- #
# Optimistic concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_stream_conflicts_when_stream_exists(driver: StoreDriver) -> None:
    await driver.append("s", [_ev("a")], expected_version=NO_STREAM)
    with pytest.raises(OptimisticConcurrencyError):
        await driver.append("s", [_ev("b")], expected_version=NO_STREAM)


@pytest.mark.asyncio
async def test_stream_exists_conflicts_when_absent(driver: StoreDriver) -> None:
    with pytest.raises(OptimisticConcurrencyError):
        await driver.append("ghost", [_ev("a")], expected_version=STREAM_EXISTS)


@pytest.mark.asyncio
async def test_exact_version_guard(driver: StoreDriver) -> None:
    await driver.append("s", [_ev("a"), _ev("b")], expected_version=NO_STREAM)
    # current version is 1; appending with expected 0 must conflict.
    with pytest.raises(OptimisticConcurrencyError):
        await driver.append("s", [_ev("c")], expected_version=0)
    # expected 1 succeeds.
    recs = await driver.append("s", [_ev("c")], expected_version=1)
    assert recs[0].version == 2


@pytest.mark.asyncio
async def test_any_never_conflicts(driver: StoreDriver) -> None:
    await driver.append("s", [_ev("a")], expected_version=NO_STREAM)
    recs = await driver.append("s", [_ev("b")], expected_version=ANY)
    assert recs[0].version == 1


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_idempotent_reappend_is_noop(driver: StoreDriver) -> None:
    e = EventData(event_type="t", payload={"k": 1}, event_id="fixed-id")
    first = await driver.append("s", [e], expected_version=NO_STREAM)
    # Re-appending the SAME event id is a no-op returning the stored event,
    # even though the expected_version no longer matches a fresh write.
    again = await driver.append("s", [e], expected_version=NO_STREAM)
    assert first[0].global_position == again[0].global_position
    assert first[0].version == again[0].version
    assert await driver.stream_version("s") == 0  # not appended twice


@pytest.mark.asyncio
async def test_empty_batch_rejected(driver: StoreDriver) -> None:
    from app.eventsourcing.store import AppendError

    with pytest.raises(AppendError):
        await driver.append("s", [], expected_version=ANY)


# --------------------------------------------------------------------------- #
# Metadata persistence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_metadata_round_trips_through_store(driver: StoreDriver) -> None:
    meta = EventMetadata(correlation_id="corr-1", causation_id="cause-1", actor="adapter")
    await driver.append(
        "s",
        [EventData(event_type="t", payload={"x": 1}, metadata=meta)],
        expected_version=NO_STREAM,
    )
    (got,) = await driver.read_stream("s")
    assert got.metadata.correlation_id == "corr-1"
    assert got.metadata.causation_id == "cause-1"
    assert got.metadata.actor == "adapter"
    assert got.payload == {"x": 1}


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_snapshot_save_and_load_latest(driver: StoreDriver) -> None:
    assert await driver.load_snapshot("s") is None
    await driver.save_snapshot(Snapshot(stream_id="s", version=2, state={"count": 3}))
    loaded = await driver.load_snapshot("s")
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.state == {"count": 3}


@pytest.mark.asyncio
async def test_snapshot_keeps_newest_version(driver: StoreDriver) -> None:
    await driver.save_snapshot(Snapshot(stream_id="s", version=5, state={"v": 5}))
    # An older snapshot must not overwrite a newer one (monotone).
    await driver.save_snapshot(Snapshot(stream_id="s", version=2, state={"v": 2}))
    loaded = await driver.load_snapshot("s")
    assert loaded is not None
    assert loaded.version == 5
    # A newer one does replace.
    await driver.save_snapshot(Snapshot(stream_id="s", version=9, state={"v": 9}))
    loaded2 = await driver.load_snapshot("s")
    assert loaded2 is not None and loaded2.version == 9


# --------------------------------------------------------------------------- #
# Transactional outbox
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_with_topic_writes_outbox_rows(driver: StoreDriver) -> None:
    await driver.append(
        "s", [_ev("a"), _ev("b")], expected_version=NO_STREAM, publish_topic="canon"
    )
    rows = await driver.outbox_rows()
    assert len(rows) == 2
    assert {r.topic for r in rows} == {"canon"}
    assert all(r.status is OutboxStatus.PENDING for r in rows)
    # Each outbox payload references its event + global position.
    assert [r.global_position for r in rows] == [1, 2]
    assert rows[0].payload["event_type"] == "a"


@pytest.mark.asyncio
async def test_append_without_topic_writes_no_outbox(driver: StoreDriver) -> None:
    await driver.append("s", [_ev("a")], expected_version=NO_STREAM)
    assert await driver.outbox_rows() == []
