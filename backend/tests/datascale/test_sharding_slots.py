"""Tests for fixed hash-slot sharding (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.slots import (
    SlotMap,
    SlotStrategy,
    migration_set,
    slot_map_covers_topology,
)
from app.datascale.sharding.strategy import ownership_distribution
from app.datascale.sharding.topology import Shard, ShardTopology


def _keys(n: int) -> list[ShardKey]:
    return [ShardKey.of(f"book-{i}") for i in range(n)]


def test_balanced_map_distributes_slots_evenly() -> None:
    m = SlotMap.balanced(["a", "b", "c"], slot_count=99)
    dist = m.distribution()
    assert dist == {"a": 33, "b": 33, "c": 33}


def test_balanced_map_handles_remainder() -> None:
    m = SlotMap.balanced(["a", "b", "c"], slot_count=100)
    dist = m.distribution()
    # 100 / 3 = 33 r1 → one shard gets 34.
    assert sorted(dist.values()) == [33, 33, 34]
    assert sum(dist.values()) == 100


def test_slot_for_and_shard_for_slot() -> None:
    m = SlotMap.balanced(["a", "b"], slot_count=16)
    assert m.shard_for_slot(0) == "a"
    assert m.shard_for_slot(15) == "b"
    with pytest.raises(ValueError):
        m.shard_for_slot(99)


def test_slot_strategy_routes_via_slot_map() -> None:
    m = SlotMap.balanced(["a", "b", "c"], slot_count=256)
    s = SlotStrategy(slot_map=m)
    for key in _keys(50):
        sid = s.route_one(key)
        assert sid in {"a", "b", "c"}
        assert s.route_one(key) == sid  # deterministic


def test_slot_strategy_spreads_keys() -> None:
    m = SlotMap.balanced(["a", "b", "c", "d"], slot_count=4096)
    s = SlotStrategy(slot_map=m)
    dist = ownership_distribution(s, _keys(8000))
    for count in dist.values():
        assert 1400 < count < 2600


def test_add_shard_moves_only_stolen_slots() -> None:
    before = SlotMap.balanced(["a", "b", "c"], slot_count=300)
    after = before.with_shard_added("d")
    # d should get ~ 300/4 = 75 slots; only those move.
    assert 60 <= after.distribution()["d"] <= 90
    migs = migration_set(before, after)
    moved = sum(m.count for m in migs)
    assert moved == after.distribution()["d"]
    # Every migration targets d (slots only flow into the new shard).
    assert all(m.target == "d" for m in migs)


def test_slot_for_key_is_stable_across_reassignment() -> None:
    # The key's *slot* never changes when slots are reassigned — only the owner.
    m1 = SlotMap.balanced(["a", "b"], slot_count=128)
    s1 = SlotStrategy(slot_map=m1)
    key = ShardKey.of("tenant-x")
    slot = s1.slot_for(key)

    m2 = m1.with_shard_added("c")
    s2 = SlotStrategy(slot_map=m2)
    assert s2.slot_for(key) == slot  # slot is invariant


def test_remove_shard_spreads_slots_over_remainder() -> None:
    before = SlotMap.balanced(["a", "b", "c"], slot_count=300)
    after = before.with_shard_removed("b")
    assert "b" not in after.shard_ids()
    assert sum(after.distribution().values()) == 300
    # b's 100 slots are redistributed to a and c.
    migs = migration_set(before, after)
    assert all(m.source == "b" for m in migs)
    assert sum(m.count for m in migs) == 100


def test_reassign_overrides_specific_slots() -> None:
    m = SlotMap.balanced(["a", "b"], slot_count=10)
    m2 = m.reassign({0: "b", 1: "b"})
    assert m2.shard_for_slot(0) == "b"
    assert m2.shard_for_slot(1) == "b"
    # Original unchanged.
    assert m.shard_for_slot(0) == "a"


def test_migration_set_groups_by_shard_pair() -> None:
    before = SlotMap(slot_count=4, assignment=("a", "a", "b", "b"))
    after = SlotMap(slot_count=4, assignment=("a", "c", "b", "c"))
    migs = migration_set(before, after)
    pairs = {(m.source, m.target): m.slots for m in migs}
    assert pairs == {("a", "c"): (1,), ("b", "c"): (3,)}


def test_migration_set_rejects_count_mismatch() -> None:
    with pytest.raises(ValueError):
        migration_set(
            SlotMap.balanced(["a"], slot_count=4),
            SlotMap.balanced(["a"], slot_count=8),
        )


def test_map_validation() -> None:
    with pytest.raises(ValueError, match="slot_count"):
        SlotMap(slot_count=0, assignment=())
    with pytest.raises(ValueError, match="assignment length"):
        SlotMap(slot_count=4, assignment=("a", "b"))


def test_cannot_remove_last_shard() -> None:
    m = SlotMap.balanced(["a"], slot_count=8)
    with pytest.raises(ValueError, match="last shard"):
        m.with_shard_removed("a")


def test_add_existing_shard_rejected() -> None:
    m = SlotMap.balanced(["a", "b"], slot_count=8)
    with pytest.raises(ValueError, match="already owns"):
        m.with_shard_added("a")


def test_slot_map_covers_topology() -> None:
    m = SlotMap.balanced(["a", "b"], slot_count=8)
    topo_ok = ShardTopology.of(
        Shard(id="a", primary_url="postgresql+asyncpg://h/a"),
        Shard(id="b", primary_url="postgresql+asyncpg://h/b"),
    )
    topo_missing = ShardTopology.of(Shard(id="a", primary_url="postgresql+asyncpg://h/a"))
    assert slot_map_covers_topology(m, topo_ok)
    assert not slot_map_covers_topology(m, topo_missing)
