"""Tests for the ShardCluster facade composing the whole stack (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.cluster import ShardCluster
from app.datascale.sharding.executor import FailureMode, FakeShardExecutor
from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    LogicalQuery,
    SortDir,
    SortKey,
)
from app.datascale.sharding.resharding import InMemoryReshardMover, ReshardPlan, ReshardState
from app.datascale.sharding.strategy import ConsistentHashStrategy, ModuloHashStrategy
from app.datascale.sharding.topology import Shard, ShardTopology

pytestmark = pytest.mark.asyncio


def _topo(*ids: str) -> ShardTopology:
    return ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}") for i in ids
    )


async def test_cluster_runs_end_to_end_scatter_query() -> None:
    topo = _topo("a", "b", "c")
    cluster = ShardCluster.build(ConsistentHashStrategy(topo, vnodes=64), topo)
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"n": 5}, {"n": 1}],
            "b": [{"n": 4}],
            "c": [{"n": 3}, {"n": 2}],
        }
    )
    query = LogicalQuery(table="t", order_by=(SortKey("n", SortDir.ASC),), limit=3)
    result = await cluster.run_query(query, fake)
    assert [r["n"] for r in result.rows] == [1, 2, 3]


async def test_cluster_aggregate_query() -> None:
    topo = _topo("a", "b")
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    fake = FakeShardExecutor(
        rows_by_shard={"a": [{"v": 1}, {"v": 2}], "b": [{"v": 3}]}
    )
    query = LogicalQuery(table="t", aggregates=(Aggregate(AggregateOp.COUNT),))
    result = await cluster.run_query(query, fake)
    assert result.rows[0]["count_star"] == 3


async def test_cluster_single_shard_fast_path() -> None:
    topo = _topo("a", "b", "c")
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    plan = cluster.plan(LogicalQuery(table="t", shard_key="book-7"))
    assert plan.is_single_shard


async def test_reshard_overlay_auto_publishes_into_router() -> None:
    topo = _topo("old", "new")
    # Use a directory-free modulo strategy and move a specific key.
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    key = ShardKey.of("moving")
    base = cluster.router().route(key).single
    target = "new" if base == "old" else "old"

    mover = InMemoryReshardMover(
        data={
            base: {"books": {"r1": (key, "payload")}},
            target: {"books": {}},
        }
    )
    plan = ReshardPlan(table="books", keys=(key,), source=base, target=target, batch_size=10)
    job = cluster.begin_reshard(plan, mover)

    # Before starting: routing is to the base shard.
    assert cluster.router().route(key).single == base

    # Drive dual-write: writes now fan to both homes, reads stay on base.
    await job.begin_dual_write()
    wres = cluster.router().route(key, access=_write())
    assert set(wres.shard_ids) == {base, target}
    assert cluster.router().route(key).single == base  # read still base

    # Finish the reshard: router cuts over to the target automatically.
    await job.backfill()
    await job.verify()
    await job.cutover()
    assert cluster.router().route(key).single == target

    await job.cleanup()
    assert job.state is ReshardState.DONE
    # Overlay retired: routing falls back to the strategy (still base for this key).
    assert cluster.router().route(key).single == base


def _write():  # type: ignore[no-untyped-def]
    from app.datascale.sharding.router import Access

    return Access.WRITE


async def test_cluster_partial_failure_mode() -> None:
    topo = _topo("a", "b", "c")
    cluster = ShardCluster.build(ModuloHashStrategy(topo), topo)
    fake = FakeShardExecutor(
        rows_by_shard={"a": [{"id": 1}], "b": [{"id": 2}], "c": [{"id": 3}]},
        fail_shards=frozenset({"b"}),
    )
    result = await cluster.run_query(
        LogicalQuery(table="t"), fake, failure_mode=FailureMode.PARTIAL
    )
    assert result.partial
    assert sorted(r["id"] for r in result.rows) == [1, 3]
