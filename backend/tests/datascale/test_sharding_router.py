"""Unit tests for the state-aware shard router (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.router import (
    Access,
    MigrationOverlay,
    ShardRouter,
)
from app.datascale.sharding.strategy import (
    ModuloHashStrategy,
    RangeBound,
    RangeStrategy,
    RoutingError,
)
from app.datascale.sharding.topology import Shard, ShardState, ShardTopology


def _topo(*specs: tuple[str, ShardState]) -> ShardTopology:
    return ShardTopology.from_iterable(
        Shard(id=i, primary_url=f"postgresql+asyncpg://h/{i}", state=st) for i, st in specs
    )


def _active_topo(*ids: str) -> ShardTopology:
    return _topo(*((i, ShardState.ACTIVE) for i in ids))


def _key_routing_to(strat: ModuloHashStrategy, shard_id: str) -> ShardKey:
    """Find a deterministic key that the strategy routes to ``shard_id``."""
    for i in range(10_000):
        k = ShardKey.of(f"k{i}")
        if strat.route_one(k) == shard_id:
            return k
    raise AssertionError(f"no key routes to {shard_id!r}")


def test_single_key_read_routes_to_one_shard() -> None:
    topo = _active_topo("a", "b", "c")
    router = ShardRouter(ModuloHashStrategy(topo), topo)
    res = router.route("book-1", access=Access.READ)
    assert not res.scatter
    assert res.single in {"a", "b", "c"}
    assert res.key == ShardKey.of("book-1")


def test_write_to_readonly_shard_raises() -> None:
    # Force a key onto a READ_ONLY shard and assert the write is refused.
    topo = _topo(("a", ShardState.READ_ONLY), ("b", ShardState.ACTIVE))
    strat = ModuloHashStrategy(topo)
    router = ShardRouter(strat, topo)
    key = _key_routing_to(strat, "a")
    with pytest.raises(RoutingError, match="does not accept writes"):
        router.route(key, access=Access.WRITE)
    # The same key reads fine (READ_ONLY accepts reads).
    assert router.route(key, access=Access.READ).single == "a"


def test_read_from_offline_shard_raises() -> None:
    topo = _topo(("a", ShardState.OFFLINE), ("b", ShardState.ACTIVE))
    strat = ModuloHashStrategy(topo)
    router = ShardRouter(strat, topo)
    key = _key_routing_to(strat, "a")
    with pytest.raises(RoutingError, match="does not accept reads"):
        router.route(key, access=Access.READ)


def test_range_query_resolves_to_touched_shards() -> None:
    topo = _active_topo("s1", "s2", "s3")
    strat = RangeStrategy(
        bounds=(
            RangeBound("s1", None, 100),
            RangeBound("s2", 100, 200),
            RangeBound("s3", 200, None),
        )
    )
    router = ShardRouter(strat, topo)
    res = router.route_range(50, 150, access=Access.READ)
    assert set(res.shard_ids) == {"s1", "s2"}
    assert res.scatter


def test_scatter_all_excludes_unreadable() -> None:
    topo = _topo(("a", ShardState.ACTIVE), ("b", ShardState.OFFLINE), ("c", ShardState.ACTIVE))
    strat = ModuloHashStrategy(topo)
    router = ShardRouter(strat, topo)
    res = router.scatter_all(access=Access.READ)
    assert set(res.shard_ids) == {"a", "c"}  # offline 'b' excluded


def test_scatter_all_write_excludes_nonwritable() -> None:
    topo = _topo(("a", ShardState.ACTIVE), ("b", ShardState.READ_ONLY))
    strat = ModuloHashStrategy(topo)
    router = ShardRouter(strat, topo)
    res = router.scatter_all(access=Access.WRITE)
    assert set(res.shard_ids) == {"a"}  # read_only 'b' not writable


def test_migration_overlay_dual_write() -> None:
    topo = _active_topo("old", "new")
    strat = ModuloHashStrategy(topo)
    # Pin a key's base to "old" by constructing an overlay regardless of hash.
    key = ShardKey.of("moving-tenant")
    base = strat.route_one(key)
    other = "new" if base == "old" else "old"
    overlay = MigrationOverlay(moves={key: (base, other, False)})
    router = ShardRouter(strat, topo, overlay=overlay)

    # Dual-write window: write hits both homes.
    wres = router.route(key, access=Access.WRITE)
    assert set(wres.shard_ids) == {base, other}
    assert wres.scatter
    # Read still goes to the source (authoritative pre-cutover).
    rres = router.route(key, access=Access.READ)
    assert rres.single == base


def test_migration_overlay_after_cutover() -> None:
    topo = _active_topo("old", "new")
    strat = ModuloHashStrategy(topo)
    key = ShardKey.of("moving-tenant")
    base = strat.route_one(key)
    other = "new" if base == "old" else "old"
    overlay = MigrationOverlay(moves={key: (base, other, True)})  # cutover done
    router = ShardRouter(strat, topo, overlay=overlay)

    # After cutover both read and write go to the target only.
    assert router.route(key, access=Access.READ).single == other
    assert router.route(key, access=Access.WRITE).single == other


def test_resolution_single_raises_on_scatter() -> None:
    topo = _active_topo("a", "b")
    router = ShardRouter(ModuloHashStrategy(topo), topo)
    res = router.scatter_all()
    with pytest.raises(RoutingError, match="single-shard"):
        _ = res.single
