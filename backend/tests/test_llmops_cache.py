"""Unit tests for the response cache (no infra)."""

from __future__ import annotations

from typing import Any

from app.llmops.cache import InMemoryBackend, ResponseCache, cache_key


def test_cache_key_stable_and_order_independent() -> None:
    k1 = cache_key(prompt_key="a", prompt_version="1.0.0", model="m", inputs={"x": 1, "y": 2})
    k2 = cache_key(prompt_key="a", prompt_version="1.0.0", model="m", inputs={"y": 2, "x": 1})
    assert k1 == k2  # dict key order does not change the key


def test_cache_key_differs_by_version() -> None:
    base: dict[str, Any] = {"prompt_key": "a", "model": "m", "inputs": {"x": 1}}
    assert cache_key(prompt_version="1.0.0", **base) != cache_key(prompt_version="2.0.0", **base)


def test_cache_key_float_rounding() -> None:
    k1 = cache_key(prompt_key="a", prompt_version="1", model="m", inputs={}, temperature=0.10000001)
    k2 = cache_key(prompt_key="a", prompt_version="1", model="m", inputs={}, temperature=0.1)
    assert k1 == k2


async def test_get_or_set_runs_producer_once() -> None:
    cache = ResponseCache()
    calls = {"n": 0}

    async def producer() -> str:
        calls["n"] += 1
        return "VALUE"

    kw: dict[str, Any] = {
        "prompt_key": "a",
        "prompt_version": "1.0.0",
        "model": "m",
        "inputs": {"x": 1},
    }
    v1, hit1 = await cache.get_or_set(producer, **kw)
    v2, hit2 = await cache.get_or_set(producer, **kw)
    assert (v1, v2) == ("VALUE", "VALUE")
    assert hit1 is False and hit2 is True
    assert calls["n"] == 1


def test_stats() -> None:
    cache = ResponseCache()
    kw: dict[str, Any] = {
        "prompt_key": "a",
        "prompt_version": "1.0.0",
        "model": "m",
        "inputs": {"x": 1},
    }
    assert cache.get(**kw) is None  # miss
    cache.set("v", **kw)
    assert cache.get(**kw) == "v"  # hit
    stats = cache.stats()
    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.hit_rate == 0.5


def test_lru_eviction() -> None:
    backend = InMemoryBackend(max_entries=2)
    backend.set("a", "1", None)
    backend.set("b", "2", None)
    backend.get("a")  # touch a -> b is now LRU
    backend.set("c", "3", None)  # evicts b
    assert backend.get("a") == "1"
    assert backend.get("b") is None
    assert backend.get("c") == "3"


def test_ttl_expiry() -> None:
    clock = {"t": 0.0}
    backend = InMemoryBackend(_clock=lambda: clock["t"])
    backend.set("k", "v", ttl_s=10.0)
    assert backend.get("k") == "v"
    clock["t"] = 11.0
    assert backend.get("k") is None  # expired


def test_clear() -> None:
    cache = ResponseCache()
    cache.set("v", prompt_key="a", prompt_version="1", model="m", inputs={})
    cache.clear()
    assert cache.stats().size == 0
    assert cache.stats().hits == 0
