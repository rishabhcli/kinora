"""Postgres integration tests: bulk-load, the EXPLAIN inspector, online DDL, health.

Uses dedicated throwaway tables on their own metadata; SKIPs when
``KINORA_TEST_DATABASE_URL`` is unset. Runs against ``kinora_dblayer_test`` :5433.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Integer, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.db.bulk import bulk_insert, bulk_insert_returning, bulk_upsert
from app.db.engine import EngineConfig, EngineRegistry
from app.db.health import engine_health, ping, pool_stats, registry_health
from app.db.inspect import (
    explain,
    explain_analyze,
    pg_stat_statements_available,
    recent_slow_queries,
    top_statements,
)
from app.db.migration_safety import (
    add_nullable_column_sql,
    backfill_in_batches,
    create_index_concurrently_sql,
    set_not_null_via_check_sql,
)

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)


class _Base(DeclarativeBase):
    pass


class Item(_Base):
    __tablename__ = "test_bulk_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    qty: Mapped[int] = mapped_column(Integer, default=0)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def session(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    db = maker()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()


# --- bulk-load --------------------------------------------------------------


async def test_bulk_insert_chunks(session: AsyncSession) -> None:
    rows = [{"id": f"i{n:04d}", "name": "x", "qty": n} for n in range(250)]
    inserted = await bulk_insert(session, Item, rows, chunk_rows=100)
    assert inserted == 250
    count = (await session.execute(select(Item))).scalars().all()
    assert len(count) == 250


async def test_bulk_insert_empty(session: AsyncSession) -> None:
    assert await bulk_insert(session, Item, []) == 0


async def test_bulk_upsert_do_update(session: AsyncSession) -> None:
    await bulk_insert(session, Item, [{"id": "a", "name": "old", "qty": 1}])
    affected = await bulk_upsert(
        session,
        Item,
        [{"id": "a", "name": "new", "qty": 9}, {"id": "b", "name": "fresh", "qty": 2}],
        conflict_columns=["id"],
        update_columns=["name", "qty"],
    )
    assert affected == 2
    a = await session.get(Item, "a")
    assert a is not None and a.name == "new" and a.qty == 9
    assert (await session.get(Item, "b")) is not None


async def test_bulk_upsert_do_nothing(session: AsyncSession) -> None:
    await bulk_insert(session, Item, [{"id": "a", "name": "keep", "qty": 1}])
    affected = await bulk_upsert(
        session,
        Item,
        [{"id": "a", "name": "ignored", "qty": 99}],
        conflict_columns=["id"],
    )
    # DO NOTHING → the existing row is untouched.
    a = await session.get(Item, "a")
    assert a is not None and a.name == "keep"
    assert affected == 0


async def test_bulk_upsert_requires_conflict_columns(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="conflict_columns"):
        await bulk_upsert(session, Item, [{"id": "x", "name": "y"}], conflict_columns=[])


async def test_bulk_insert_returning_ids(session: AsyncSession) -> None:
    ids = await bulk_insert_returning(
        session,
        Item,
        [{"id": "r1", "name": "a"}, {"id": "r2", "name": "b"}],
        returning="id",
    )
    assert set(ids) == {"r1", "r2"}


# --- EXPLAIN inspector ------------------------------------------------------


async def test_explain_parses_plan(session: AsyncSession) -> None:
    await bulk_insert(session, Item, [{"id": f"e{n}", "name": "x", "qty": n} for n in range(20)])
    plan = await explain(session, select(Item).where(Item.id == "e1"))
    assert plan.total_cost >= 0
    assert plan.node_types()  # non-empty
    d = plan.as_dict()
    assert "used_seq_scan" in d
    assert "risks" in d


async def test_explain_analyze_executes_and_times(session: AsyncSession) -> None:
    await bulk_insert(session, Item, [{"id": f"a{n}", "name": "x", "qty": n} for n in range(5)])
    plan = await explain_analyze(session, select(Item))
    # ANALYZE populates execution time + actual rows.
    assert plan.execution_time_ms is not None
    assert any(n.actual_rows is not None for n in plan.root.walk())


async def test_explain_engine_bind(maker: async_sessionmaker[AsyncSession]) -> None:
    # explain() also accepts an AsyncEngine bind (opens its own connection).
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    try:
        plan = await explain(engine, text("SELECT 1"))
        assert plan.total_cost >= 0
    finally:
        await engine.dispose()


async def test_pg_stat_statements_probe_does_not_raise(session: AsyncSession) -> None:
    # Whether or not the extension is installed, the probe + read must be safe.
    available = await pg_stat_statements_available(session)
    assert isinstance(available, bool)
    stats = await top_statements(session, limit=5)
    assert isinstance(stats, list)
    if not available:
        assert stats == []


# --- online DDL + backfill --------------------------------------------------


async def test_expand_backfill_contract_lifecycle(maker: async_sessionmaker[AsyncSession]) -> None:
    # Seed rows.
    async with maker() as db:
        await bulk_insert(db, Item, [{"id": f"b{n:03d}", "name": "x", "qty": n} for n in range(15)])
        await db.commit()

    # Expand: add a nullable column.
    async with maker() as db:
        await db.execute(text(add_nullable_column_sql("test_bulk_items", "qty2", "integer")))
        await db.commit()

    # Backfill it in batches keyed by the monotonic id, committing each batch.
    async with maker() as db:
        report = await backfill_in_batches(
            db,
            """
            UPDATE test_bulk_items SET qty2 = qty
            WHERE id IN (
                SELECT id FROM test_bulk_items
                WHERE qty2 IS NULL AND id > :after
                ORDER BY id LIMIT :limit
            )
            RETURNING id
            """,
            batch_size=4,
        )
        assert report.done is True
        assert report.rows_updated == 15
        assert report.batches >= 4  # 15 rows / 4 per batch + terminator

    # Contract: enforce NOT NULL via the check-then-validate path.
    async with maker() as db:
        for stmt in set_not_null_via_check_sql("test_bulk_items", "qty2"):
            await db.execute(text(stmt))
        await db.commit()

    # Verify the column is now NOT NULL by attempting a NULL insert.
    async with maker() as db:
        with pytest.raises(Exception):  # noqa: B017 - any IntegrityError flavour
            await db.execute(text("INSERT INTO test_bulk_items (id, name) VALUES ('nullrow', 'n')"))
        await db.rollback()


async def test_create_index_concurrently_outside_txn(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    sql = create_index_concurrently_sql(
        index_name="ix_test_bulk_items_qty", table="test_bulk_items", columns=["qty"]
    )
    try:
        # CONCURRENTLY must run on an autocommit connection (no open txn).
        async with engine.connect() as conn:
            ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await ac.execute(text(sql))
            # Idempotent: IF NOT EXISTS makes a re-run a no-op.
            await ac.execute(text(sql))
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_test_bulk_items_qty'")
                )
            ).first()
            assert exists is not None
            await conn.execute(text("DROP INDEX IF EXISTS ix_test_bulk_items_qty"))
            await conn.commit()
    finally:
        await engine.dispose()


# --- health -----------------------------------------------------------------


async def test_ping_and_pool_stats() -> None:
    assert _DB_URL is not None
    reg = EngineRegistry(primary_config=EngineConfig(url=_DB_URL))
    try:
        engine = reg.writer()
        assert await ping(engine, timeout_s=5.0) is True
        stats = pool_stats(engine)
        assert stats.pool_class in {"AsyncAdaptedQueuePool", "NullPool"}
        health = await engine_health(engine, role="primary")
        assert health.alive is True
        assert health.latency_ms is not None and health.latency_ms >= 0
    finally:
        await reg.dispose()


async def test_registry_health_aggregate() -> None:
    assert _DB_URL is not None
    reg = EngineRegistry(primary_config=EngineConfig(url=_DB_URL, slow_query_ms=0.0))
    try:
        report = await registry_health(reg, timeout_s=5.0)
        assert report["ok"] is True
        assert report["engines"][0]["role"] == "primary"
        # Slow-query counters are folded in when the primary is instrumented.
        assert "slow_queries" in report
    finally:
        await reg.dispose()


async def test_ping_failure_returns_false() -> None:
    # A bad host resolves to a down engine; ping must report False, never raise.
    engine = create_async_engine(
        "postgresql+asyncpg://nouser:nopass@127.0.0.1:1/nodb", poolclass=NullPool
    )
    try:
        assert await ping(engine, timeout_s=1.0) is False
    finally:
        await engine.dispose()


async def test_recent_slow_queries_feed() -> None:
    assert _DB_URL is not None
    # Threshold 0 → every query is captured into the ring buffer.
    reg = EngineRegistry(primary_config=EngineConfig(url=_DB_URL, slow_query_ms=0.001))
    try:
        engine = reg.writer()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(0.01)"))
        feed = recent_slow_queries(engine)
        assert any("pg_sleep" in r.statement for r in feed)
    finally:
        await reg.dispose()
