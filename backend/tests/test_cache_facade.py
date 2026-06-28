"""Unit tests for the :class:`Cache` facade — the behaviour call sites rely on.

Covers cache-aside, read-through (get_or_load), write-through, negative caching,
key + tag invalidation, fail-open on backend errors, and namespacing. All
in-memory, deterministic via :class:`FakeClock`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from app.cache.cache import Cache, CacheConfig
from app.cache.clock import FakeClock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.interface import CacheBackend
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics

pytestmark = pytest.mark.asyncio


def _cache(
    *,
    clock: FakeClock,
    config: CacheConfig | None = None,
    metrics: CacheMetrics | None = None,
    max_entries: int = 1024,
) -> Cache[Any]:
    m = metrics or CacheMetrics()
    backend = MemoryCache(max_entries=max_entries, clock=clock, metrics=m, metrics_namespace="ns")
    return Cache(backend, namespace="ns", config=config, clock=clock, metrics=m)


# --------------------------------------------------------------------------- #
# Cache-aside
# --------------------------------------------------------------------------- #


async def test_get_miss_returns_default() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    assert await c.get("k") is None
    assert await c.get("k", default="x") == "x"
    assert c.stats().misses == 2


async def test_set_then_get_hit() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set("k", {"v": 1})
    assert await c.get("k") == {"v": 1}
    assert c.stats().hits == 1
    assert c.stats().sets == 1


async def test_get_expired_is_miss() -> None:
    clk = FakeClock()
    c = _cache(clock=clk, config=CacheConfig(namespace="ns", default_ttl=10.0))
    await c.set("k", "v")
    clk.advance(11.0)
    assert await c.get("k") is None
    assert c.stats().misses == 1


async def test_explicit_ttl_none_means_no_expiry() -> None:
    clk = FakeClock()
    c = _cache(clock=clk, config=CacheConfig(namespace="ns", default_ttl=1.0))
    await c.set("k", "v", ttl=None)
    clk.advance(1_000_000.0)
    assert await c.get("k") == "v"


async def test_has() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    assert await c.has("k") is False
    await c.set("k", 1, ttl=5.0)
    assert await c.has("k") is True
    clk.advance(6.0)
    assert await c.has("k") is False


# --------------------------------------------------------------------------- #
# Read-through (get_or_load)
# --------------------------------------------------------------------------- #


async def test_get_or_load_caches_loader_result() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert await c.get_or_load("k", loader) == 42
    assert await c.get_or_load("k", loader) == 42
    assert calls == 1  # second call served from cache
    assert c.stats().loads == 1
    assert c.stats().hits == 1
    assert c.stats().misses == 1


async def test_get_or_load_reloads_after_expiry() -> None:
    clk = FakeClock()
    c = _cache(clock=clk, config=CacheConfig(namespace="ns", default_ttl=10.0))
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await c.get_or_load("k", loader) == 1
    clk.advance(11.0)
    assert await c.get_or_load("k", loader) == 2
    assert calls == 2


async def test_get_or_load_propagates_loader_error_and_counts() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)

    async def boom() -> int:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await c.get_or_load("k", boom)
    assert c.stats().load_errors == 1


# --------------------------------------------------------------------------- #
# Negative caching
# --------------------------------------------------------------------------- #


async def test_negative_cache_avoids_repeat_loads() -> None:
    clk = FakeClock()
    c = _cache(
        clock=clk,
        config=CacheConfig(namespace="ns", cache_negatives=True, negative_ttl=30.0),
    )
    calls = 0

    async def loader() -> None:
        nonlocal calls
        calls += 1
        return None  # the "absent" sentinel

    assert await c.get_or_load("k", loader) is None
    assert await c.get_or_load("k", loader) is None
    assert calls == 1  # absence was cached
    assert c.stats().negative_hits == 1


async def test_negative_cache_expires_and_reloads() -> None:
    clk = FakeClock()
    c = _cache(
        clock=clk,
        config=CacheConfig(namespace="ns", cache_negatives=True, negative_ttl=30.0),
    )
    calls = 0

    async def loader() -> Any:
        nonlocal calls
        calls += 1
        return None if calls == 1 else "found"

    assert await c.get_or_load("k", loader) is None
    clk.advance(31.0)
    assert await c.get_or_load("k", loader) == "found"
    assert calls == 2


async def test_negative_cache_can_be_disabled_per_call() -> None:
    clk = FakeClock()
    c = _cache(clock=clk, config=CacheConfig(namespace="ns", cache_negatives=True))
    calls = 0

    async def loader() -> None:
        nonlocal calls
        calls += 1
        return None

    await c.get_or_load("k", loader, cache_negatives=False)
    await c.get_or_load("k", loader, cache_negatives=False)
    assert calls == 2  # absence NOT cached


async def test_set_negative_directly() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set_negative("k", ttl=5.0)
    entry = await c.get_entry("k")
    assert entry is not None
    assert entry.negative
    assert await c.get("k") is None


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #


async def test_invalidate_key() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set("k", 1)
    assert await c.invalidate("k") == 1
    assert await c.get("k") is None
    assert await c.invalidate("k") == 0


async def test_invalidate_many_keys() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set("a", 1)
    await c.set("b", 2)
    assert await c.invalidate("a", "b", "c") == 2


async def test_invalidate_tag_is_the_cheap_edit_primitive() -> None:
    # The §8.7 story: tag entries by the character they reference; changing one
    # character invalidates only that tag, everything else still hits.
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set("shot1", "clip1", tags=["char:alice"])
    await c.set("shot2", "clip2", tags=["char:alice", "char:bob"])
    await c.set("shot3", "clip3", tags=["char:bob"])
    removed = await c.invalidate_tag("char:alice")
    assert removed == 2
    assert await c.get("shot1") is None
    assert await c.get("shot2") is None
    assert await c.get("shot3") == "clip3"  # untouched


async def test_tags_are_namespaced() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    backend = MemoryCache(clock=clk, metrics=m)
    a: Cache[Any] = Cache(backend, namespace="A", clock=clk, metrics=m)
    b: Cache[Any] = Cache(backend, namespace="B", clock=clk, metrics=m)
    await a.set("k", 1, tags=["hot"])
    await b.set("k", 2, tags=["hot"])
    # Invalidating A's "hot" tag must not touch B's "hot" entry.
    await a.invalidate_tag("hot")
    assert await a.get("k") is None
    assert await b.get("k") == 2


async def test_invalidate_namespace_clears_backend() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    await c.set("a", 1)
    await c.set("b", 2)
    await c.invalidate_namespace()
    assert await c.get("a") is None
    assert await c.get("b") is None


# --------------------------------------------------------------------------- #
# Fail-open on backend errors
# --------------------------------------------------------------------------- #


class _FlakyBackend(CacheBackend):
    name = "flaky"

    async def get(self, key: str) -> CacheEntry | None:
        raise CacheBackendError("down")

    async def set(self, key: str, entry: CacheEntry) -> None:
        raise CacheBackendError("down")

    async def delete(self, key: str) -> bool:
        raise CacheBackendError("down")

    async def delete_many(self, keys: Iterable[str]) -> int:
        raise CacheBackendError("down")

    async def clear(self) -> None:
        raise CacheBackendError("down")

    async def delete_tag(self, tag: str) -> int:
        raise CacheBackendError("down")

    async def health(self) -> bool:
        return False


async def test_fail_open_degrades_to_miss() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    c: Cache[Any] = Cache(
        _FlakyBackend(),
        namespace="ns",
        config=CacheConfig(namespace="ns", fail_open=True),
        clock=clk,
        metrics=m,
    )
    # Reads return default, writes are swallowed, loader still runs each time.
    assert await c.get("k") is None
    await c.set("k", 1)

    async def loader() -> int:
        return 7

    assert await c.get_or_load("k", loader) == 7
    assert m.stats("ns").backend_errors >= 1


async def test_fail_closed_raises() -> None:
    clk = FakeClock()
    c: Cache[Any] = Cache(
        _FlakyBackend(),
        namespace="ns",
        config=CacheConfig(namespace="ns", fail_open=False),
        clock=clk,
    )
    with pytest.raises(CacheBackendError):
        await c.get("k")


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


async def test_namespace_property_overrides_config() -> None:
    clk = FakeClock()
    backend = MemoryCache(clock=clk)
    c: Cache[Any] = Cache(
        backend, namespace="explicit", config=CacheConfig(namespace="other"), clock=clk
    )
    assert c.namespace == "explicit"


async def test_health_delegates_to_backend() -> None:
    clk = FakeClock()
    c = _cache(clock=clk)
    assert await c.health() is True
