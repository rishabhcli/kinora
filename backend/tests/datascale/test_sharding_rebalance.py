"""Tests for the rebalance planner (no infra)."""

from __future__ import annotations

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.rebalance import (
    plan_add_shard_slots,
    plan_remove_shard_slots,
    plan_ring_rebalance,
    plan_slot_rebalance,
    topology_delta,
)
from app.datascale.sharding.slots import SlotMap
from app.datascale.sharding.strategy import ConsistentHashStrategy
from app.datascale.sharding.topology import Shard, ShardTopology


def _topo(*ids: str) -> ShardTopology:
    return ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}") for i in ids
    )


def _keys(n: int) -> list[ShardKey]:
    return [ShardKey.of(f"k{i}") for i in range(n)]


def test_slot_rebalance_plan_is_exact() -> None:
    before = SlotMap.balanced(["a", "b", "c"], slot_count=300)
    plan, after = plan_add_shard_slots(before, "d")
    # Total moved equals the new shard's slot count (only stolen slots move).
    assert plan.total_units_moved == after.distribution()["d"]
    assert not plan.is_noop
    # Inflow lands entirely on d.
    assert set(plan.per_shard_inflow().keys()) == {"d"}


def test_remove_shard_plan() -> None:
    before = SlotMap.balanced(["a", "b", "c"], slot_count=300)
    plan, after = plan_remove_shard_slots(before, "b")
    assert plan.total_units_moved == 100  # b's slots
    assert set(plan.per_shard_outflow().keys()) == {"b"}
    assert "b" not in after.shard_ids()


def test_slot_rebalance_noop_when_identical() -> None:
    m = SlotMap.balanced(["a", "b"], slot_count=64)
    plan = plan_slot_rebalance(m, m)
    assert plan.is_noop
    assert plan.total_units_moved == 0


def test_ring_rebalance_estimates_minimal_movement() -> None:
    keys = _keys(4000)
    before = ConsistentHashStrategy(_topo("a", "b", "c"), vnodes=256)
    after = ConsistentHashStrategy(_topo("a", "b", "c", "d"), vnodes=256)
    plan = plan_ring_rebalance(before, after, sample_keys=keys)
    # Consistent hashing moves ~1/4 when going 3→4 shards; assert well under half.
    assert plan.total_units_moved < len(keys) * 0.45
    assert not plan.is_noop
    # Most inflow lands on the new shard d.
    inflow = plan.per_shard_inflow()
    assert inflow.get("d", 0) > plan.total_units_moved * 0.6


def test_ring_rebalance_explain_renders() -> None:
    keys = _keys(500)
    before = ConsistentHashStrategy(_topo("a", "b"), vnodes=128)
    after = ConsistentHashStrategy(_topo("a", "b", "c"), vnodes=128)
    plan = plan_ring_rebalance(before, after, sample_keys=keys)
    text = plan.explain()
    assert "RebalancePlan" in text
    assert "keys:" in text


def test_topology_delta() -> None:
    added, removed = topology_delta(_topo("a", "b"), _topo("b", "c", "d"))
    assert set(added) == {"c", "d"}
    assert set(removed) == {"a"}


def test_inflow_outflow_balanced_for_slot_move() -> None:
    before = SlotMap.balanced(["a", "b"], slot_count=100)
    plan, _after = plan_add_shard_slots(before, "c")
    inflow = plan.per_shard_inflow()
    outflow = plan.per_shard_outflow()
    # Conservation: total in == total out == total moved.
    assert sum(inflow.values()) == plan.total_units_moved
    assert sum(outflow.values()) == plan.total_units_moved
