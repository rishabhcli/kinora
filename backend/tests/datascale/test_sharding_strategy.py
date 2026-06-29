"""Unit tests for the four shard-placement strategies (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.strategy import (
    ConsistentHashStrategy,
    DirectoryStrategy,
    ModuloHashStrategy,
    RangeBound,
    RangeStrategy,
    RoutingError,
    ownership_distribution,
)
from app.datascale.sharding.topology import Shard, ShardTopology


def _topo(*ids: str) -> ShardTopology:
    return ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}") for i in ids
    )


def _keys(n: int, prefix: str = "book-") -> list[ShardKey]:
    return [ShardKey.of(f"{prefix}{i}") for i in range(n)]


# --- modulo hash ----------------------------------------------------------- #


def test_modulo_hash_is_deterministic_and_in_set() -> None:
    s = ModuloHashStrategy(_topo("a", "b", "c"))
    for key in _keys(50):
        sid = s.route_one(key)
        assert sid in {"a", "b", "c"}
        assert s.route_one(key) == sid  # stable


def test_modulo_hash_spreads_keys() -> None:
    s = ModuloHashStrategy(_topo("a", "b", "c", "d"))
    dist = ownership_distribution(s, _keys(4000))
    # Every shard gets a meaningful share (roughly 1000 each); allow slack.
    for count in dist.values():
        assert 600 < count < 1400


def test_modulo_hash_range_is_full_scatter() -> None:
    s = ModuloHashStrategy(_topo("a", "b"))
    assert set(s.route_range("a", "z")) == {"a", "b"}


def test_modulo_hash_empty_topology_rejected() -> None:
    with pytest.raises(ValueError):
        ModuloHashStrategy(_topo())


# --- range ----------------------------------------------------------------- #


def _int_ranges() -> RangeStrategy:
    return RangeStrategy(
        bounds=(
            RangeBound("s1", None, 100),
            RangeBound("s2", 100, 200),
            RangeBound("s3", 200, None),
        )
    )


def test_range_routes_to_owning_bound() -> None:
    s = _int_ranges()
    assert s.route_one(ShardKey.of(0)) == "s1"
    assert s.route_one(ShardKey.of(99)) == "s1"
    assert s.route_one(ShardKey.of(100)) == "s2"  # lower-inclusive
    assert s.route_one(ShardKey.of(199)) == "s2"
    assert s.route_one(ShardKey.of(200)) == "s3"
    assert s.route_one(ShardKey.of(10_000)) == "s3"
    assert s.route_one(ShardKey.of(-5)) == "s1"


def test_range_query_prunes_to_touched_shards() -> None:
    s = _int_ranges()
    assert set(s.route_range(0, 50)) == {"s1"}
    assert set(s.route_range(50, 150)) == {"s1", "s2"}
    assert set(s.route_range(150, 250)) == {"s2", "s3"}
    assert set(s.route_range(None, None)) == {"s1", "s2", "s3"}
    # Exclusive upper landing exactly on a boundary does not touch the next shard.
    assert set(s.route_range(0, 100)) == {"s1"}


def test_range_validation_gap_rejected() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        RangeStrategy(
            bounds=(
                RangeBound("s1", None, 100),
                RangeBound("s2", 150, None),  # gap 100..150
            )
        )


def test_range_validation_first_must_be_neg_inf() -> None:
    with pytest.raises(ValueError, match="-inf"):
        RangeStrategy(bounds=(RangeBound("s1", 0, None),))


def test_range_validation_last_must_be_pos_inf() -> None:
    with pytest.raises(ValueError, match=r"\+inf"):
        RangeStrategy(bounds=(RangeBound("s1", None, 100),))


def test_range_validation_lower_ge_upper_rejected() -> None:
    with pytest.raises(ValueError, match="lower"):
        RangeStrategy(
            bounds=(
                RangeBound("s1", None, 100),
                RangeBound("s2", 100, 100),
                RangeBound("s3", 100, None),
            )
        )


def test_range_string_keys() -> None:
    s = RangeStrategy(
        bounds=(
            RangeBound("s1", None, "m"),
            RangeBound("s2", "m", None),
        )
    )
    assert s.route_one(ShardKey.of("apple")) == "s1"
    assert s.route_one(ShardKey.of("zebra")) == "s2"
    assert s.route_one(ShardKey.of("m")) == "s2"


# --- directory ------------------------------------------------------------- #


def test_directory_pins_and_falls_back() -> None:
    fallback = ModuloHashStrategy(_topo("a", "b"))
    pinned = ShardKey.of("vip-tenant")
    d = DirectoryStrategy(table={pinned: "hot-shard"}, fallback=fallback)
    assert d.route_one(pinned) == "hot-shard"
    # An unpinned key uses the fallback.
    other = ShardKey.of("normal-tenant")
    assert d.route_one(other) == fallback.route_one(other)
    assert "hot-shard" in d.all_shards()
    assert "a" in d.all_shards() and "b" in d.all_shards()


def test_directory_no_fallback_raises_on_miss() -> None:
    d = DirectoryStrategy(table={ShardKey.of("x"): "s1"})
    with pytest.raises(RoutingError):
        d.route_one(ShardKey.of("unmapped"))


def test_directory_with_and_without_entry_are_pure() -> None:
    fallback = ModuloHashStrategy(_topo("a", "b"))
    d0 = DirectoryStrategy(table={}, fallback=fallback)
    key = ShardKey.of("t1")
    d1 = d0.with_entry(key, "a")
    assert d1.route_one(key) == "a"
    assert d0.table == {}  # original unchanged
    d2 = d1.without_entry(key)
    assert d2.route_one(key) == fallback.route_one(key)
    assert d1.route_one(key) == "a"  # d1 unchanged


# --- consistent hashing ---------------------------------------------------- #


def test_consistent_hash_deterministic_and_in_set() -> None:
    s = ConsistentHashStrategy(_topo("a", "b", "c"), vnodes=64)
    for key in _keys(50):
        sid = s.route_one(key)
        assert sid in {"a", "b", "c"}
        assert s.route_one(key) == sid


def test_consistent_hash_spreads_keys_with_vnodes() -> None:
    s = ConsistentHashStrategy(_topo("a", "b", "c", "d"), vnodes=256)
    dist = ownership_distribution(s, _keys(8000))
    # With 256 vnodes the distribution is reasonably balanced.
    for count in dist.values():
        assert 1200 < count < 2800


def test_consistent_hash_minimal_movement_on_add() -> None:
    keys = _keys(5000)
    before = ConsistentHashStrategy(_topo("a", "b", "c"), vnodes=256)
    after = ConsistentHashStrategy(_topo("a", "b", "c", "d"), vnodes=256)
    moved = sum(1 for k in keys if before.route_one(k) != after.route_one(k))
    # Adding a 4th shard should remap roughly 1/4 of keys, far below the ~75%
    # a modulo-hash would churn. Assert it stays well under half.
    assert moved < len(keys) * 0.45
    # And the keys that moved should mostly land on the new shard.
    landed_on_new = sum(
        1 for k in keys if before.route_one(k) != after.route_one(k) and after.route_one(k) == "d"
    )
    assert landed_on_new > moved * 0.7


def test_modulo_hash_churns_almost_everything_on_add() -> None:
    # Contrast test: modulo-hash remaps the overwhelming majority on a resize.
    keys = _keys(3000)
    before = ModuloHashStrategy(_topo("a", "b", "c"))
    after = ModuloHashStrategy(_topo("a", "b", "c", "d"))
    moved = sum(1 for k in keys if before.route_one(k) != after.route_one(k))
    assert moved > len(keys) * 0.6  # ~3/4 churn


def test_consistent_hash_weighting_biases_ownership() -> None:
    topo = ShardTopology.of(
        Shard(id="big", primary_url="postgresql+asyncpg://h/big", weight=4),
        Shard(id="small", primary_url="postgresql+asyncpg://h/small", weight=1),
    )
    s = ConsistentHashStrategy(topo, vnodes=256)
    dist = ownership_distribution(s, _keys(8000))
    assert dist["big"] > dist["small"] * 2  # heavily biased toward the big box


def test_consistent_hash_replicas_distinct_and_ordered() -> None:
    s = ConsistentHashStrategy(_topo("a", "b", "c", "d"), vnodes=64)
    key = ShardKey.of("book-77")
    reps = s.route_replicas(key, 3)
    assert len(reps) == 3
    assert len(set(reps)) == 3  # distinct
    assert reps[0] == s.route_one(key)  # primary first


def test_consistent_hash_replicas_capped_at_shard_count() -> None:
    s = ConsistentHashStrategy(_topo("a", "b"), vnodes=64)
    reps = s.route_replicas(ShardKey.of("x"), 5)
    assert set(reps) == {"a", "b"}  # only two shards exist


def test_consistent_hash_describe_and_ring_size() -> None:
    s = ConsistentHashStrategy(_topo("a", "b"), vnodes=100)
    assert s.ring_size() == 200
    desc = s.describe()
    assert desc["kind"] == "consistent_hash"
    assert desc["ring_size"] == 200
