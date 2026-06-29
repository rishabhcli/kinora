"""Postgres-integration tests for the sharding production adapters.

These prove the *real* SQL paths the fakes stand in for: the session-backed
scatter-gather executor and a real-data resharding move. A multi-shard fleet is
simulated with **separate schemas** on one throwaway test DB on :5433 — each
"shard" is a schema, which is a faithful, deterministic stand-in for separate
clusters for the routing/merge/resharding logic (the only thing that differs in
production is the connection URL per shard, which the adapters already handle).

SKIP cleanly when ``KINORA_TEST_DATABASE_URL`` is unset. Never touches the live
``kinora`` DB (the README/MEMORY rule: isolated DB on :5433).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.datascale.sharding.cluster import ShardCluster
from app.datascale.sharding.executor import FailureMode, Row
from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    LogicalQuery,
    ShardSubquery,
    SortDir,
    SortKey,
)
from app.datascale.sharding.resharding import (
    ReshardDataMover,
    ReshardingJob,
    ReshardPlan,
    ReshardState,
)
from app.datascale.sharding.strategy import ModuloHashStrategy
from app.datascale.sharding.topology import Shard, ShardTopology

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

_SHARDS = ("shard_a", "shard_b", "shard_c")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    eng = create_async_engine(_DB_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        for schema in _SHARDS:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.execute(
                text(
                    f"CREATE TABLE {schema}.books "
                    "(id TEXT PRIMARY KEY, book_id TEXT, score INT)"
                )
            )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            for schema in _SHARDS:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await eng.dispose()


class _SchemaShardExecutor:
    """A ShardExecutor running real SQL against each shard's schema on one engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def fetch(
        self, shard_id: str, subquery: ShardSubquery, query: LogicalQuery
    ) -> list[Row]:
        parts: list[str]
        if query.aggregates:
            select_list = ", ".join(self._agg_sql(a) for a in query.aggregates)
            group = ", ".join(query.group_by)
            parts = [f"SELECT {group + ', ' if group else ''}{select_list} FROM {shard_id}.books"]
            if group:
                parts.append(f"GROUP BY {group}")
        else:
            parts = [f"SELECT * FROM {shard_id}.books"]
            if query.order_by:
                order = ", ".join(
                    f"{s.field} {s.direction.value.upper()}" for s in query.order_by
                )
                parts.append(f"ORDER BY {order}")
            if subquery.per_shard_limit is not None:
                parts.append(f"LIMIT {subquery.per_shard_limit}")
        sql = " ".join(parts)
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql))
            return [dict(r._mapping) for r in result]  # noqa: SLF001

    @staticmethod
    def _agg_sql(agg: Aggregate) -> str:
        out_name = agg.output_name
        if agg.op is AggregateOp.COUNT:
            inner = "*" if agg.field is None else agg.field
            return f"COUNT({inner}) AS {out_name}"
        if agg.op is AggregateOp.SUM:
            return f"SUM({agg.field}) AS {out_name}"
        if agg.op is AggregateOp.MIN:
            return f"MIN({agg.field}) AS {out_name}"
        if agg.op is AggregateOp.MAX:
            return f"MAX({agg.field}) AS {out_name}"
        raise AssertionError(f"unsupported agg in test executor: {agg.op}")


class _SchemaReshardMover(ReshardDataMover):
    """A real-data resharding mover over per-shard schemas on one engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @staticmethod
    def _key_ids(keys: Any) -> list[str]:
        return [str(k.single_value) for k in keys]

    async def count(self, shard_id: str, table: str, keys: Any) -> int:
        ids = self._key_ids(keys)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {shard_id}.{table} WHERE book_id = ANY(:ids)"),
                {"ids": ids},
            )
            return int(result.scalar() or 0)

    async def copy_batch(
        self, source: str, target: str, table: str, keys: Any, *, offset: int, limit: int
    ) -> int:
        ids = self._key_ids(keys)
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT id, book_id, score FROM {source}.{table} "
                        "WHERE book_id = ANY(:ids) ORDER BY id "
                        "OFFSET :off LIMIT :lim"
                    ),
                    {"ids": ids, "off": offset, "lim": limit},
                )
            ).fetchall()
            for row in rows:
                await conn.execute(
                    text(
                        f"INSERT INTO {target}.{table} (id, book_id, score) "
                        "VALUES (:id, :book_id, :score) "
                        "ON CONFLICT (id) DO UPDATE SET book_id = EXCLUDED.book_id, "
                        "score = EXCLUDED.score"
                    ),
                    {"id": row.id, "book_id": row.book_id, "score": row.score},
                )
            return len(rows)

    async def checksum(self, shard_id: str, table: str, keys: Any) -> str:
        ids = self._key_ids(keys)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT md5(string_agg(id || ':' || score, ',' ORDER BY id)) "
                    f"FROM {shard_id}.{table} WHERE book_id = ANY(:ids)"
                ),
                {"ids": ids},
            )
            return str(result.scalar() or "")

    async def delete_batch(self, shard_id: str, table: str, keys: Any, *, limit: int) -> int:
        ids = self._key_ids(keys)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"DELETE FROM {shard_id}.{table} WHERE id IN "
                    f"(SELECT id FROM {shard_id}.{table} WHERE book_id = ANY(:ids) "
                    "ORDER BY id LIMIT :lim)"
                ),
                {"ids": ids, "lim": limit},
            )
            return result.rowcount or 0


def _topo() -> ShardTopology:
    assert _DB_URL is not None
    return ShardTopology.from_iterable(
        Shard(id=s, primary_url=_DB_URL) for s in _SHARDS
    )


async def _insert(engine: AsyncEngine, shard: str, rid: str, book_id: str, score: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO {shard}.books (id, book_id, score) VALUES (:i, :b, :s)"),
            {"i": rid, "b": book_id, "s": score},
        )


async def test_scatter_gather_topn_against_real_postgres(engine: AsyncEngine) -> None:
    # Seed each shard with rows; the global top-3 by score spans shards.
    await _insert(engine, "shard_a", "a1", "bk1", 50)
    await _insert(engine, "shard_a", "a2", "bk1", 90)
    await _insert(engine, "shard_b", "b1", "bk2", 100)
    await _insert(engine, "shard_c", "c1", "bk3", 70)

    topo = _topo()
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    executor = _SchemaShardExecutor(engine)
    query = LogicalQuery(table="books", order_by=(SortKey("score", SortDir.DESC),), limit=3)
    result = await cluster.run_query(query, executor)
    assert [r["score"] for r in result.rows] == [100, 90, 70]


async def test_count_aggregate_against_real_postgres(engine: AsyncEngine) -> None:
    await _insert(engine, "shard_a", "a1", "bk1", 1)
    await _insert(engine, "shard_a", "a2", "bk1", 2)
    await _insert(engine, "shard_b", "b1", "bk2", 3)
    topo = _topo()
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    executor = _SchemaShardExecutor(engine)
    result = await cluster.run_query(
        LogicalQuery(table="books", aggregates=(Aggregate(AggregateOp.COUNT),)), executor
    )
    assert result.rows[0]["count_star"] == 3


async def test_real_data_reshard_moves_rows_and_verifies(engine: AsyncEngine) -> None:
    # Two books on shard_a; move bk-move to shard_b for real.
    await _insert(engine, "shard_a", "r1", "bk-move", 10)
    await _insert(engine, "shard_a", "r2", "bk-move", 20)
    await _insert(engine, "shard_a", "r3", "bk-stay", 30)

    mover = _SchemaReshardMover(engine)
    plan = ReshardPlan(
        table="books",
        keys=(ShardKey.of("bk-move"),),
        source="shard_a",
        target="shard_b",
        batch_size=1,  # exercise batching
    )
    job = ReshardingJob(plan=plan, mover=mover)
    progress = await job.run()

    assert progress.state is ReshardState.DONE
    assert progress.verified
    assert progress.rows_backfilled == 2
    assert progress.rows_deleted == 2

    # bk-move now lives on shard_b; bk-stay untouched on shard_a.
    async with engine.connect() as conn:
        b_rows = (
            await conn.execute(text("SELECT id FROM shard_b.books ORDER BY id"))
        ).scalars().all()
        a_rows = (
            await conn.execute(text("SELECT id FROM shard_a.books ORDER BY id"))
        ).scalars().all()
    assert list(b_rows) == ["r1", "r2"]
    assert list(a_rows) == ["r3"]


async def test_partial_failure_against_real_postgres(engine: AsyncEngine) -> None:
    # Drop one shard's table so its query errors; PARTIAL mode returns the rest.
    await _insert(engine, "shard_a", "a1", "bk1", 1)
    await _insert(engine, "shard_c", "c1", "bk3", 3)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE shard_b.books"))

    topo = _topo()
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    executor = _SchemaShardExecutor(engine)
    result = await cluster.run_query(
        LogicalQuery(table="books"), executor, failure_mode=FailureMode.PARTIAL
    )
    assert result.partial
    assert {f.shard_id for f in result.failures} == {"shard_b"}
    assert sorted(r["score"] for r in result.rows) == [1, 3]
