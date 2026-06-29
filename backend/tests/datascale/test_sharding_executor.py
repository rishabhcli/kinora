"""Tests for the scatter-gather executor's merge correctness (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.executor import (
    FailureMode,
    FakeShardExecutor,
    ScatterGatherExecutor,
)
from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    LogicalQuery,
    QueryPlanner,
    SortDir,
    SortKey,
)
from app.datascale.sharding.router import ShardRouter
from app.datascale.sharding.strategy import ModuloHashStrategy
from app.datascale.sharding.topology import Shard, ShardTopology

pytestmark = pytest.mark.asyncio


def _planner(*ids: str) -> QueryPlanner:
    topo = ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}") for i in ids
    )
    return QueryPlanner(ShardRouter(ModuloHashStrategy(topo), topo))


async def test_passthrough_single_shard() -> None:
    planner = _planner("a", "b", "c")
    plan = planner.plan(LogicalQuery(table="shots", shard_key="book-1"))
    target = plan.shard_ids[0]
    fake = FakeShardExecutor(rows_by_shard={target: [{"id": 1}, {"id": 2}]})
    res = await ScatterGatherExecutor(fake).execute(plan)
    assert res.rows == [{"id": 1}, {"id": 2}]
    assert not res.partial


async def test_concat_unordered_scatter() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(LogicalQuery(table="books"))
    fake = FakeShardExecutor(
        rows_by_shard={"a": [{"id": 1}], "b": [{"id": 2}, {"id": 3}]}
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    ids = sorted(r["id"] for r in res.rows)
    assert ids == [1, 2, 3]


async def test_merge_sort_global_topn_across_shards() -> None:
    # The crucial case: the global top-3 by score is NOT each shard's top-1.
    planner = _planner("a", "b", "c")
    plan = planner.plan(
        LogicalQuery(
            table="ratings",
            order_by=(SortKey("score", SortDir.DESC),),
            limit=3,
        )
    )
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"id": "a1", "score": 100}, {"id": "a2", "score": 99}, {"id": "a3", "score": 98}],
            "b": [{"id": "b1", "score": 50}],
            "c": [{"id": "c1", "score": 10}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    assert [r["id"] for r in res.rows] == ["a1", "a2", "a3"]  # all from shard a


async def test_merge_sort_ascending() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(
        LogicalQuery(table="t", order_by=(SortKey("n", SortDir.ASC),), limit=4)
    )
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"n": 1}, {"n": 3}, {"n": 5}],
            "b": [{"n": 2}, {"n": 4}, {"n": 6}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    assert [r["n"] for r in res.rows] == [1, 2, 3, 4]


async def test_merge_sort_with_offset() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(
        LogicalQuery(table="t", order_by=(SortKey("n"),), limit=2, offset=2)
    )
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"n": 1}, {"n": 3}, {"n": 5}],
            "b": [{"n": 2}, {"n": 4}, {"n": 6}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    # Global order is 1,2,3,4,5,6; skip 2 -> [3,4].
    assert [r["n"] for r in res.rows] == [3, 4]


async def test_count_aggregate_sums_partials() -> None:
    planner = _planner("a", "b", "c")
    plan = planner.plan(LogicalQuery(table="books", aggregates=(Aggregate(AggregateOp.COUNT),)))
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"x": 1}, {"x": 2}],
            "b": [{"x": 3}],
            "c": [{"x": 4}, {"x": 5}, {"x": 6}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    assert res.rows[0]["count_star"] == 6


async def test_sum_min_max_aggregates() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(
        LogicalQuery(
            table="t",
            aggregates=(
                Aggregate(AggregateOp.SUM, field="v", alias="total"),
                Aggregate(AggregateOp.MIN, field="v", alias="lo"),
                Aggregate(AggregateOp.MAX, field="v", alias="hi"),
            ),
        )
    )
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"v": 10}, {"v": 20}],
            "b": [{"v": 5}, {"v": 100}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    row = res.rows[0]
    assert row["total"] == 135
    assert row["lo"] == 5
    assert row["hi"] == 100


async def test_avg_recombined_correctly() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(
        LogicalQuery(table="t", aggregates=(Aggregate(AggregateOp.AVG, field="v", alias="avg_v"),))
    )
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"v": 10}, {"v": 20}],  # sum 30 cnt 2
            "b": [{"v": 30}, {"v": 40}, {"v": 100}],  # sum 170 cnt 3
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    # Global avg = (30+170)/(2+3) = 200/5 = 40 — NOT the mean of per-shard means.
    assert res.rows[0]["avg_v"] == 40


async def test_group_aggregate_regroups_across_shards() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(
        LogicalQuery(
            table="events",
            aggregates=(Aggregate(AggregateOp.COUNT, field="id", alias="n"),),
            group_by=("kind",),
        )
    )
    # Same group "click" appears on both shards; must be summed.
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"kind": "click", "id": 1}, {"kind": "view", "id": 2}],
            "b": [{"kind": "click", "id": 3}, {"kind": "click", "id": 4}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    by_kind = {r["kind"]: r["n"] for r in res.rows}
    assert by_kind == {"click": 3, "view": 1}


async def test_partial_mode_records_failures_and_returns_rest() -> None:
    planner = _planner("a", "b", "c")
    plan = planner.plan(LogicalQuery(table="books"))
    fake = FakeShardExecutor(
        rows_by_shard={"a": [{"id": 1}], "b": [{"id": 2}], "c": [{"id": 3}]},
        fail_shards=frozenset({"b"}),
    )
    res = await ScatterGatherExecutor(fake, FailureMode.PARTIAL).execute(plan)
    assert res.partial
    assert {f.shard_id for f in res.failures} == {"b"}
    assert sorted(r["id"] for r in res.rows) == [1, 3]
    assert res.shards_queried == 3
    assert res.shards_succeeded == 2


async def test_fail_fast_raises_on_shard_error() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(LogicalQuery(table="books"))
    fake = FakeShardExecutor(
        rows_by_shard={"a": [{"id": 1}], "b": [{"id": 2}]},
        fail_shards=frozenset({"a"}),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await ScatterGatherExecutor(fake, FailureMode.FAIL_FAST).execute(plan)


async def test_merge_sort_handles_none_values_last() -> None:
    planner = _planner("a", "b")
    plan = planner.plan(LogicalQuery(table="t", order_by=(SortKey("n"),), limit=4))
    fake = FakeShardExecutor(
        rows_by_shard={
            "a": [{"n": 1}, {"n": None}],
            "b": [{"n": 2}, {"n": None}],
        }
    )
    res = await ScatterGatherExecutor(fake).execute(plan)
    ns = [r["n"] for r in res.rows]
    assert ns[:2] == [1, 2]
    assert ns[2] is None and ns[3] is None  # None sorts last
