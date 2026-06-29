"""Unit tests for the cross-shard query planner (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    GatherMode,
    LogicalQuery,
    QueryPlanner,
    SortDir,
    SortKey,
)
from app.datascale.sharding.router import ShardRouter
from app.datascale.sharding.strategy import ModuloHashStrategy, RangeBound, RangeStrategy
from app.datascale.sharding.topology import Shard, ShardTopology


def _topo(*ids: str) -> ShardTopology:
    return ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}") for i in ids
    )


def _hash_planner(*ids: str) -> QueryPlanner:
    topo = _topo(*ids)
    return QueryPlanner(ShardRouter(ModuloHashStrategy(topo), topo))


def _range_planner() -> QueryPlanner:
    topo = _topo("s1", "s2", "s3")
    strat = RangeStrategy(
        bounds=(
            RangeBound("s1", None, 100),
            RangeBound("s2", 100, 200),
            RangeBound("s3", 200, None),
        )
    )
    return QueryPlanner(ShardRouter(strat, topo))


def test_single_shard_query_is_passthrough() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(LogicalQuery(table="shots", shard_key="book-1"))
    assert plan.is_single_shard
    assert plan.gather_mode is GatherMode.PASSTHROUGH


def test_keyless_scatter_concats_when_unordered() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(LogicalQuery(table="books"))
    assert plan.is_scatter
    assert set(plan.shard_ids) == {"a", "b", "c"}
    assert plan.gather_mode is GatherMode.CONCAT


def test_ordered_scatter_uses_merge_sort() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(
        LogicalQuery(table="books", order_by=(SortKey("created_at", SortDir.DESC),), limit=10)
    )
    assert plan.gather_mode is GatherMode.MERGE_SORT
    # Limit push-down: each shard returns offset+limit rows.
    assert all(sq.per_shard_limit == 10 for sq in plan.subqueries)
    assert plan.global_limit == 10


def test_limit_pushdown_includes_offset() -> None:
    planner = _hash_planner("a", "b")
    plan = planner.plan(
        LogicalQuery(
            table="books", order_by=(SortKey("name"),), limit=5, offset=20
        )
    )
    # Each shard must surface offset+limit = 25 rows for the global merge.
    assert all(sq.per_shard_limit == 25 for sq in plan.subqueries)
    assert plan.global_offset == 20
    assert plan.global_limit == 5
    # The per-shard offset is always 0 (offset applied globally at gather).
    assert all(sq.per_shard_offset == 0 for sq in plan.subqueries)


def test_count_is_aggregate_mode_no_limit() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(
        LogicalQuery(table="books", aggregates=(Aggregate(AggregateOp.COUNT),))
    )
    assert plan.gather_mode is GatherMode.AGGREGATE
    # Aggregates scan everything: no limit push-down.
    assert all(sq.per_shard_limit is None for sq in plan.subqueries)


def test_avg_is_rewritten_to_sum_and_count() -> None:
    planner = _hash_planner("a", "b")
    plan = planner.plan(
        LogicalQuery(
            table="ratings",
            aggregates=(Aggregate(AggregateOp.AVG, field="score", alias="avg_score"),),
        )
    )
    ops = [a.op for a in plan.effective_aggregates]
    assert AggregateOp.SUM in ops and AggregateOp.COUNT in ops
    assert AggregateOp.AVG not in ops  # rewritten away


def test_group_by_uses_group_aggregate_mode() -> None:
    planner = _hash_planner("a", "b")
    plan = planner.plan(
        LogicalQuery(
            table="events",
            aggregates=(Aggregate(AggregateOp.COUNT),),
            group_by=("user_id",),
        )
    )
    assert plan.gather_mode is GatherMode.GROUP_AGGREGATE


def test_count_distinct_flags_holistic_warning_on_scatter() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(
        LogicalQuery(
            table="events",
            aggregates=(Aggregate(AggregateOp.COUNT_DISTINCT, field="user_id"),),
        )
    )
    assert plan.holistic_warnings
    assert "holistic" in plan.holistic_warnings[0]


def test_range_query_prunes_subqueries() -> None:
    planner = _range_planner()
    plan = planner.plan(LogicalQuery(table="ledger", key_range=(50, 150)))
    assert set(plan.shard_ids) == {"s1", "s2"}


def test_query_validation() -> None:
    with pytest.raises(ValueError, match="limit must be"):
        LogicalQuery(table="t", limit=-1)
    with pytest.raises(ValueError, match="offset must be"):
        LogicalQuery(table="t", offset=-1)
    with pytest.raises(ValueError, match="not both"):
        LogicalQuery(table="t", shard_key="x", key_range=(1, 2))
    with pytest.raises(ValueError, match="group_by requires"):
        LogicalQuery(table="t", group_by=("x",))


def test_explain_is_readable() -> None:
    planner = _hash_planner("a", "b", "c")
    plan = planner.plan(
        LogicalQuery(
            table="books",
            order_by=(SortKey("created_at", SortDir.DESC),),
            limit=10,
        )
    )
    text = plan.explain()
    assert "ScatterPlan" in text
    assert "merge_sort" in text
    assert "shard a" in text
