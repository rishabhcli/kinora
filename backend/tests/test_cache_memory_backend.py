"""Unit tests for the in-process L1 backend (LRU + TTL + tags).

Infra-free; uses a :class:`FakeClock` so TTL expiry is deterministic.
"""

from __future__ import annotations

import pytest

from app.cache.clock import FakeClock
from app.cache.entry import CacheEntry
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics

pytestmark = pytest.mark.asyncio


async def test_set_get_roundtrip() -> None:
    clk = FakeClock()
    c = MemoryCache(clock=clk)
    await c.set("k", CacheEntry.of("v", now=clk.time(), ttl=None))
    entry = await c.get("k")
    assert entry is not None
    assert entry.value == "v"


async def test_ttl_expiry_reads_as_miss_and_purges() -> None:
    clk = FakeClock()
    metrics = CacheMetrics()
    c = MemoryCache(clock=clk, metrics=metrics, metrics_namespace="ns")
    await c.set("k", CacheEntry.of("v", now=clk.time(), ttl=10.0))
    assert (await c.get("k")) is not None
    clk.advance(11.0)
    assert (await c.get("k")) is None
    # Expired entry was purged and counted.
    assert c.size() == 0
    assert metrics.stats("ns").expirations == 1


async def test_lru_eviction_drops_least_recently_used() -> None:
    clk = FakeClock()
    metrics = CacheMetrics()
    c = MemoryCache(max_entries=2, clock=clk, metrics=metrics, metrics_namespace="ns")
    await c.set("a", CacheEntry.of(1, now=clk.time()))
    await c.set("b", CacheEntry.of(2, now=clk.time()))
    # Touch "a" so "b" becomes the LRU.
    await c.get("a")
    await c.set("c", CacheEntry.of(3, now=clk.time()))
    assert (await c.get("b")) is None  # evicted
    assert (await c.get("a")) is not None
    assert (await c.get("c")) is not None
    assert metrics.stats("ns").evictions == 1


async def test_unbounded_when_max_zero() -> None:
    clk = FakeClock()
    c = MemoryCache(max_entries=0, clock=clk)
    for i in range(100):
        await c.set(f"k{i}", CacheEntry.of(i, now=clk.time()))
    assert c.size() == 100


async def test_delete_and_delete_many() -> None:
    clk = FakeClock()
    c = MemoryCache(clock=clk)
    await c.set("a", CacheEntry.of(1, now=clk.time()))
    await c.set("b", CacheEntry.of(2, now=clk.time()))
    assert await c.delete("a") is True
    assert await c.delete("a") is False
    await c.set("c", CacheEntry.of(3, now=clk.time()))
    assert await c.delete_many(["b", "c", "missing"]) == 2
    assert c.size() == 0


async def test_clear() -> None:
    clk = FakeClock()
    c = MemoryCache(clock=clk)
    await c.set("a", CacheEntry.of(1, now=clk.time(), tags=frozenset({"t"})))
    await c.clear()
    assert c.size() == 0
    assert c.tags() == []


async def test_delete_tag_drops_all_tagged_keys() -> None:
    clk = FakeClock()
    c = MemoryCache(clock=clk)
    await c.set("a", CacheEntry.of(1, now=clk.time(), tags=frozenset({"hot"})))
    await c.set("b", CacheEntry.of(2, now=clk.time(), tags=frozenset({"hot", "cold"})))
    await c.set("c", CacheEntry.of(3, now=clk.time(), tags=frozenset({"cold"})))
    removed = await c.delete_tag("hot")
    assert removed == 2
    assert (await c.get("a")) is None
    assert (await c.get("b")) is None
    assert (await c.get("c")) is not None
    # "b" should have been removed from the "cold" index too.
    assert "hot" not in c.tags()


async def test_delete_tag_unknown_is_noop() -> None:
    c = MemoryCache()
    assert await c.delete_tag("nope") == 0


async def test_get_many_set_many() -> None:
    clk = FakeClock()
    c = MemoryCache(clock=clk)
    await c.set_many(
        {
            "a": CacheEntry.of(1, now=clk.time()),
            "b": CacheEntry.of(2, now=clk.time()),
        }
    )
    got = await c.get_many(["a", "b", "missing"])
    assert set(got) == {"a", "b"}
    assert got["a"].value == 1


async def test_health_always_true() -> None:
    assert await MemoryCache().health() is True


async def test_negative_max_entries_rejected() -> None:
    with pytest.raises(ValueError):
        MemoryCache(max_entries=-1)
