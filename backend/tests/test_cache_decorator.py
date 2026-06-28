"""Tests for the ``@cached`` decorator — memoizing async functions."""

from __future__ import annotations

import pytest

from app.cache.cache import Cache, CacheConfig
from app.cache.clock import FakeClock
from app.cache.decorator import cached
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics

pytestmark = pytest.mark.asyncio


def _cache(clk: FakeClock, *, config: CacheConfig | None = None) -> Cache[object]:
    m = CacheMetrics()
    backend = MemoryCache(clock=clk, metrics=m, metrics_namespace="fn")
    return Cache(backend, namespace="fn", config=config, clock=clk, metrics=m)


async def test_memoizes_by_arguments() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c)
    async def add(a: int, b: int) -> int:
        nonlocal calls
        calls += 1
        return a + b

    assert await add(1, 2) == 3
    assert await add(1, 2) == 3  # cached
    assert await add(2, 2) == 4  # different args -> fresh
    assert calls == 2


async def test_kwargs_order_independent() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c)
    async def f(*, a: int, b: int) -> int:
        nonlocal calls
        calls += 1
        return a + b

    await f(a=1, b=2)
    await f(b=2, a=1)
    assert calls == 1


async def test_exclude_kwarg_from_key() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c, exclude=["session"])
    async def f(x: int, *, session: str) -> int:
        nonlocal calls
        calls += 1
        return x

    await f(1, session="s1")
    await f(1, session="s2")  # session excluded -> same key
    assert calls == 1


async def test_ttl_override_expires() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c, ttl=10.0)
    async def f(x: int) -> int:
        nonlocal calls
        calls += 1
        return x

    await f(1)
    clk.advance(11.0)
    await f(1)
    assert calls == 2


async def test_static_tags_enable_bulk_invalidation() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c, tags=["group"])
    async def f(x: int) -> int:
        nonlocal calls
        calls += 1
        return x

    await f(1)
    await f(2)
    assert calls == 2
    await c.invalidate_tag("group")
    await f(1)
    await f(2)
    assert calls == 4  # both re-loaded after the tag was dropped


async def test_dynamic_tags_depend_on_args() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c, tags=lambda entity_id, *_a, **_kw: [f"entity:{entity_id}"])
    async def embed(entity_id: str, version: int) -> str:
        nonlocal calls
        calls += 1
        return f"{entity_id}-v{version}"

    await embed("alice", 1)
    await embed("bob", 1)
    await c.invalidate_tag("entity:alice")
    await embed("alice", 1)  # re-loaded
    await embed("bob", 1)  # still cached
    assert calls == 3


async def test_custom_key_builder() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c, key=lambda user, **_: f"user:{user}")
    async def lookup(user: str, trace_id: str) -> str:
        nonlocal calls
        calls += 1
        return user

    await lookup("u1", trace_id="a")
    await lookup("u1", trace_id="b")  # trace_id ignored by the key
    assert calls == 1


async def test_attached_helpers_cache_key_and_invalidate() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0

    @cached(c)
    async def f(x: int) -> int:
        nonlocal calls
        calls += 1
        return x

    await f(5)
    key = f.cache_key(5)
    assert isinstance(key, str)
    assert await f.invalidate(5) is True
    await f(5)
    assert calls == 2  # invalidation forced a reload
    assert f.cache is c


async def test_negative_caching_through_decorator() -> None:
    clk = FakeClock()
    c = _cache(clk, config=CacheConfig(namespace="fn", cache_negatives=True, negative_ttl=30.0))
    calls = 0

    @cached(c)
    async def maybe(x: int) -> int | None:
        nonlocal calls
        calls += 1
        return None

    assert await maybe(1) is None
    assert await maybe(1) is None
    assert calls == 1  # absence cached
