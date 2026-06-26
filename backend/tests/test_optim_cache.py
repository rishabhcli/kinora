"""Tests for app.optim.cache — content-hash memoization of deterministic results.

This is net-new: the only content-hash cache today is the ``shot_cache`` clip cache; nothing
memoizes ``CanonService.query`` / page analysis / agent outputs. ``ResultCache`` wraps a
Redis-like backend (``get_json``/``set_json``) so a re-query of identical content is served
without re-calling the model — the token/call saving on re-ingest / re-open. The mechanism is
backend-agnostic, so it tests against an in-memory fake.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.optim.cache import ResultCache, cache_key, content_hash


class FakeBackend:
    """In-memory stand-in for RedisClient (get_json/set_json with ttl_s)."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.sets: int = 0

    async def get_json(self, key: str) -> Any | None:
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        self.sets += 1
        self.store[key] = value


class ExplodingBackend(FakeBackend):
    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        raise RuntimeError("redis down")


def test_content_hash_is_deterministic_and_order_sensitive() -> None:
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("b", "a")
    assert content_hash("a", 1) != content_hash("a", 2)


def test_cache_key_includes_namespace() -> None:
    key = cache_key("canon_query", "book1", 42)
    assert key.startswith("kinora:optim:cache:canon_query:")


async def test_get_or_compute_misses_then_hits_calling_factory_once() -> None:
    backend = FakeBackend()
    cache = ResultCache(backend, namespace="canon_query")
    calls = 0

    async def factory() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": 7}

    first = await cache.get_or_compute(["book1", 42], factory)
    second = await cache.get_or_compute(["book1", 42], factory)
    assert first == {"value": 7}
    assert second == {"value": 7}
    assert calls == 1  # the second call was served from cache — the token/call saving
    assert cache.stats.hits == 1 and cache.stats.misses == 1


async def test_get_or_compute_distinct_keys_do_not_collide() -> None:
    backend = FakeBackend()
    cache = ResultCache(backend, namespace="page")
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    a = await cache.get_or_compute(["p1"], factory)
    b = await cache.get_or_compute(["p2"], factory)
    assert a == 1 and b == 2
    assert calls == 2


async def test_disabled_cache_always_computes_and_never_touches_backend() -> None:
    backend = FakeBackend()
    cache = ResultCache(backend, namespace="x", enabled=False)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "v"

    await cache.get_or_compute(["k"], factory)
    await cache.get_or_compute(["k"], factory)
    assert calls == 2
    assert backend.sets == 0 and backend.store == {}


async def test_none_result_is_cached_distinctly_from_a_miss() -> None:
    backend = FakeBackend()
    cache = ResultCache(backend, namespace="x")
    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1
        return None

    assert await cache.get_or_compute(["k"], factory) is None
    assert await cache.get_or_compute(["k"], factory) is None
    assert calls == 1  # None was cached (envelope), so the second call is a hit


async def test_serialize_deserialize_roundtrip() -> None:
    backend = FakeBackend()
    cache = ResultCache(
        backend,
        namespace="model",
        serialize=lambda obj: {"n": obj["n"]},
        deserialize=lambda raw: {"n": raw["n"], "rebuilt": True},
    )
    calls = 0

    async def factory() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"n": 5}

    await cache.get_or_compute(["k"], factory)  # miss -> serialize+store
    hit = await cache.get_or_compute(["k"], factory)  # hit -> deserialize
    assert hit == {"n": 5, "rebuilt": True}
    assert calls == 1


async def test_backend_write_failure_never_breaks_the_call() -> None:
    cache = ResultCache(ExplodingBackend(), namespace="x")
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    # set_json raises, but the computed value is still returned.
    assert await cache.get_or_compute(["k"], factory) == "ok"
    assert calls == 1


async def test_hit_rate_reports_fraction() -> None:
    backend = FakeBackend()
    cache = ResultCache(backend, namespace="x")

    async def factory() -> int:
        return 1

    await cache.get_or_compute(["k"], factory)  # miss
    await cache.get_or_compute(["k"], factory)  # hit
    await cache.get_or_compute(["k"], factory)  # hit
    assert cache.stats.hit_rate == pytest.approx(2 / 3)
