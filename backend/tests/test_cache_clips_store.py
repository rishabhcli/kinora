"""Object-store clip tier + tier-stack assembly.

Covers the durable L3 backend (sidecar round-trip, in-band TTL reap, tag-index
deletion, fail-open wrapping) and the L1->L2->L3 stack assembly + promotion. All
deterministic: a dict-backed :class:`InMemoryClipStore` and a :class:`FakeClock`,
no network.
"""

from __future__ import annotations

import pytest

from app.cache.clips.store import (
    SIDECAR_PREFIX,
    InMemoryClipStore,
    ObjectStoreCacheBackend,
)
from app.cache.clips.tiers import build_clip_backend
from app.cache.clock import FakeClock
from app.cache.entry import CacheEntry
from app.cache.errors import CacheBackendError
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics
from app.cache.tiered import TieredCache

pytestmark = pytest.mark.asyncio


def _entry(
    value: object, *, now: float, ttl: float | None = None, tags: list[str] | None = None
) -> CacheEntry:
    return CacheEntry.of(value, now=now, ttl=ttl, tags=frozenset(tags or []))


async def test_object_backend_round_trip() -> None:
    clk = FakeClock()
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=clk)
    await backend.set("rk1:abc", _entry({"clip_key": "clips/a.mp4"}, now=clk.time()))
    assert store.size() == 1
    assert store.keys()[0].startswith(SIDECAR_PREFIX)
    got = await backend.get("rk1:abc")
    assert got is not None and got.value == {"clip_key": "clips/a.mp4"}


async def test_object_backend_miss_is_none() -> None:
    backend = ObjectStoreCacheBackend(InMemoryClipStore(), clock=FakeClock())
    assert await backend.get("rk1:nope") is None


async def test_object_backend_ttl_reaped_in_band() -> None:
    clk = FakeClock()
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=clk)
    await backend.set("rk1:k", _entry({"v": 1}, now=clk.time(), ttl=50.0))
    assert await backend.get("rk1:k") is not None
    clk.advance(51.0)
    assert await backend.get("rk1:k") is None
    # The expired sidecar is lazily reaped on read.
    assert store.size() == 0


async def test_object_backend_delete() -> None:
    clk = FakeClock()
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=clk)
    await backend.set("rk1:k", _entry({"v": 1}, now=clk.time()))
    assert await backend.delete("rk1:k") is True
    assert await backend.delete("rk1:k") is False
    assert await backend.get("rk1:k") is None


async def test_object_backend_tag_index_and_delete_tag() -> None:
    clk = FakeClock()
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=clk)
    await backend.set("rk1:a", _entry({"v": 1}, now=clk.time(), tags=["entity:7"]))
    await backend.set("rk1:b", _entry({"v": 2}, now=clk.time(), tags=["entity:7"]))
    await backend.set("rk1:c", _entry({"v": 3}, now=clk.time(), tags=["entity:9"]))
    removed = await backend.delete_tag("entity:7")
    assert removed == 2
    assert await backend.get("rk1:a") is None
    assert await backend.get("rk1:b") is None
    assert await backend.get("rk1:c") is not None
    # Unknown tag is a no-op.
    assert await backend.delete_tag("entity:404") == 0


async def test_object_backend_clear_is_noop_durable() -> None:
    clk = FakeClock()
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=clk)
    await backend.set("rk1:k", _entry({"v": 1}, now=clk.time()))
    await backend.clear()  # durable tier intentionally persists through a clear
    assert await backend.get("rk1:k") is not None


async def test_object_backend_wraps_io_errors_as_backend_error() -> None:
    class Boom(InMemoryClipStore):
        def get_bytes(self, key: str) -> bytes:
            raise RuntimeError("disk on fire")

    store = Boom()
    backend = ObjectStoreCacheBackend(store, clock=FakeClock())
    # Seed directly so exists() is True but get_bytes() raises.
    store._objects[backend._sidecar_key("rk1:k")] = b"{}"
    with pytest.raises(CacheBackendError):
        await backend.get("rk1:k")


async def test_object_backend_corrupt_payload_raises_backend_error() -> None:
    store = InMemoryClipStore()
    backend = ObjectStoreCacheBackend(store, clock=FakeClock())
    store._objects[backend._sidecar_key("rk1:k")] = b"not json"
    with pytest.raises(CacheBackendError):
        await backend.get("rk1:k")


async def test_build_backend_l1_only_with_no_infra() -> None:
    backend = build_clip_backend(
        namespace="ns", metrics=CacheMetrics(), clock=FakeClock()
    )
    assert isinstance(backend, MemoryCache)


async def test_build_backend_l1_plus_object() -> None:
    store = InMemoryClipStore()
    backend = build_clip_backend(
        namespace="ns",
        metrics=CacheMetrics(),
        clock=FakeClock(),
        object_store=store,
    )
    # L1 in front of the durable object tier.
    assert isinstance(backend, TieredCache)
    assert isinstance(backend.l1, MemoryCache)
    assert isinstance(backend.l2, ObjectStoreCacheBackend)


async def test_build_backend_three_tier_nesting_with_redis_and_object() -> None:
    # A trivial fake redis that the RedisCache constructor accepts (never called
    # because reads hit L1/L3 here). Only the *shape* is asserted.
    store = InMemoryClipStore()
    backend = build_clip_backend(
        namespace="ns",
        metrics=CacheMetrics(),
        clock=FakeClock(),
        redis=object(),
        object_store=store,
    )
    assert isinstance(backend, TieredCache)
    assert isinstance(backend.l1, MemoryCache)
    # Lower stack is itself a Tiered(L2, L3).
    assert isinstance(backend.l2, TieredCache)


async def test_tier_promotion_object_to_l1() -> None:
    clk = FakeClock()
    metrics = CacheMetrics()
    store = InMemoryClipStore()
    backend = build_clip_backend(
        namespace="ns", metrics=metrics, clock=clk, object_store=store
    )
    assert isinstance(backend, TieredCache)
    # Seed only the durable tier directly.
    await backend.l2.set("rk1:k", _entry({"v": 7}, now=clk.time()))
    assert await backend.l1.get("rk1:k") is None
    # A read through the stack promotes the L3 hit into L1.
    got = await backend.get("rk1:k")
    assert got is not None and got.value == {"v": 7}
    assert await backend.l1.get("rk1:k") is not None
    assert metrics.stats("ns").l2_hits >= 1


async def test_in_memory_store_counts_and_urls() -> None:
    store = InMemoryClipStore(bucket="b")
    store.put_bytes("k", b"x")
    assert store.exists("k")
    assert store.get_bytes("k") == b"x"
    assert store.presigned_get_url("k") == "memory://b/k"
    store.delete("k")
    assert not store.exists("k")
    assert store.put_calls == 1 and store.get_calls == 1 and store.delete_calls == 1
