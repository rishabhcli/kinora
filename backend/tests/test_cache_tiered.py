"""Tests for the tiered (L1+L2) backend and the in-memory factories.

L2 is faked with a second :class:`MemoryCache` so the tier semantics
(promotion, write-through, fan-out delete, fail-open) are exercised without
Redis.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from app.cache.clock import FakeClock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.factory import CacheManager, memory_cache, null_cache
from app.cache.interface import CacheBackend
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics
from app.cache.tiered import TieredCache

pytestmark = pytest.mark.asyncio


def _tiered(clk: FakeClock, metrics: CacheMetrics, *, fail_open: bool = True) -> TieredCache:
    l1 = MemoryCache(clock=clk, metrics=metrics, metrics_namespace="t")
    l2 = MemoryCache(clock=clk, metrics=metrics, metrics_namespace="t-l2")
    return TieredCache(l1, l2, clock=clk, metrics=metrics, metrics_namespace="t",
                       l2_fail_open=fail_open)


async def test_l1_hit_short_circuits_l2() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    t = _tiered(clk, m)
    await t.set("k", CacheEntry.of("v", now=clk.time()))
    entry = await t.get("k")
    assert entry is not None and entry.value == "v"
    assert m.stats("t").l1_hits == 1


async def test_l2_hit_promotes_into_l1() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    t = _tiered(clk, m)
    # Seed only L2 directly.
    await t.l2.set("k", CacheEntry.of("v", now=clk.time(), ttl=100.0))
    assert await t.l1.get("k") is None
    entry = await t.get("k")
    assert entry is not None and entry.value == "v"
    assert m.stats("t").l2_hits == 1
    # Now promoted: a second read is an L1 hit.
    assert await t.l1.get("k") is not None


async def test_write_through_writes_both_tiers() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    t = _tiered(clk, m)
    await t.set("k", CacheEntry.of("v", now=clk.time()))
    assert await t.l1.get("k") is not None
    assert await t.l2.get("k") is not None


async def test_delete_fans_out() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    t = _tiered(clk, m)
    await t.set("k", CacheEntry.of("v", now=clk.time()))
    assert await t.delete("k") is True
    assert await t.l1.get("k") is None
    assert await t.l2.get("k") is None


async def test_delete_tag_fans_out() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    t = _tiered(clk, m)
    await t.set("k", CacheEntry.of("v", now=clk.time(), tags=frozenset({"tag"})))
    removed = await t.delete_tag("tag")
    assert removed >= 1
    assert await t.l1.get("k") is None
    assert await t.l2.get("k") is None


class _DownL2(CacheBackend):
    name = "down"

    async def get(self, key: str) -> CacheEntry | None:
        raise CacheBackendError("l2 down")

    async def set(self, key: str, entry: CacheEntry) -> None:
        raise CacheBackendError("l2 down")

    async def delete(self, key: str) -> bool:
        raise CacheBackendError("l2 down")

    async def delete_many(self, keys: Iterable[str]) -> int:
        raise CacheBackendError("l2 down")

    async def clear(self) -> None:
        raise CacheBackendError("l2 down")

    async def delete_tag(self, tag: str) -> int:
        raise CacheBackendError("l2 down")

    async def health(self) -> bool:
        return False


async def test_fail_open_degrades_to_l1_only() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    l1 = MemoryCache(clock=clk, metrics=m, metrics_namespace="t")
    t = TieredCache(l1, _DownL2(), clock=clk, metrics=m, metrics_namespace="t", l2_fail_open=True)
    # Writes still land in L1; reads from L1 still work; L2 errors are counted.
    await t.set("k", CacheEntry.of("v", now=clk.time()))
    entry = await t.get("k")
    assert entry is not None and entry.value == "v"
    assert m.stats("t").backend_errors >= 1
    # Health is L1-only under fail-open.
    assert await t.health() is True


async def test_fail_closed_propagates_l2_error() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    l1 = MemoryCache(clock=clk, metrics=m, metrics_namespace="t")
    t = TieredCache(l1, _DownL2(), clock=clk, metrics=m, metrics_namespace="t", l2_fail_open=False)
    with pytest.raises(CacheBackendError):
        await t.set("k", CacheEntry.of("v", now=clk.time()))


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #


async def test_memory_cache_factory_works_with_no_infra() -> None:
    c = memory_cache(namespace="x")
    await c.set("k", 1)
    assert await c.get("k") == 1


async def test_null_cache_never_stores() -> None:
    c = null_cache(namespace="x")
    await c.set("k", 1)
    assert await c.get("k") is None

    async def loader() -> int:
        return 7

    assert await c.get_or_load("k", loader) == 7
    assert await c.get("k") is None  # nothing was retained


async def test_cache_manager_memoizes_namespaces_no_redis() -> None:
    mgr = CacheManager()  # no redis -> memory-only
    assert mgr.has_redis is False
    a1 = mgr.get("ns-a")
    a2 = mgr.get("ns-a")
    b = mgr.get("ns-b")
    assert a1 is a2  # same instance memoized
    assert a1 is not b
    await a1.set("k", 1)
    assert await mgr.get("ns-a").get("k") == 1  # shared instance
    assert set(mgr.namespaces()) == {"ns-a", "ns-b"}


async def test_cache_manager_snapshot_reports_per_namespace() -> None:
    mgr = CacheManager()
    c = mgr.get("ns")
    await c.set("k", 1)
    await c.get("k")
    await c.get("missing")
    snap = mgr.snapshot()
    assert "ns" in snap
    assert snap["ns"]["hits"] == 1
    assert snap["ns"]["misses"] == 1


async def test_cache_manager_shared_metrics_across_namespaces() -> None:
    mgr = CacheManager()
    a = mgr.get("a")
    b = mgr.get("b")
    await a.set("k", 1)
    await b.set("k", 2)
    # Each namespace's metrics are independent but live in the shared bag.
    assert a.metrics is b.metrics is mgr.metrics
    assert mgr.metrics.stats("a").sets == 1
    assert mgr.metrics.stats("b").sets == 1


async def test_cache_manager_close_is_safe() -> None:
    mgr = CacheManager()
    mgr.get("a")
    await mgr.close()
