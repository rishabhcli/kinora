"""Integration-helper tests + public package-surface smoke test.

The composition helpers must work with no infra (in-memory fallback) so a
``Settings`` with an empty/absent ``redis_url`` yields a usable cache.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import app.cache as cache_pkg
from app.cache import (
    CacheManager,
    build_cache_manager,
    build_cache_manager_from_settings,
    redis_lock_factory,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeSettings:
    redis_url: str | None = None


async def test_build_manager_in_memory_when_no_redis() -> None:
    mgr = build_cache_manager(redis_url=None)
    assert isinstance(mgr, CacheManager)
    assert mgr.has_redis is False
    c = mgr.get("ns")
    await c.set("k", 1)
    assert await c.get("k") == 1


async def test_build_from_settings_empty_url_is_in_memory() -> None:
    mgr = build_cache_manager_from_settings(_FakeSettings(redis_url=""))
    assert mgr.has_redis is False
    c = mgr.get("ns")
    await c.set("k", "v")
    assert await c.get("k") == "v"


async def test_build_from_settings_missing_attr_is_in_memory() -> None:
    mgr = build_cache_manager_from_settings(object())
    assert mgr.has_redis is False


async def test_redis_lock_factory_wraps_client_lock() -> None:
    seen: dict[str, object] = {}

    class _Lock:
        async def __aenter__(self) -> _Lock:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _Client:
        def lock(self, name: str, **kw: object) -> _Lock:
            seen["name"] = name
            seen["kw"] = kw
            return _Lock()

    factory = redis_lock_factory(_Client())
    lock = factory("ns:lock:k")
    async with lock:
        pass
    assert seen["name"] == "ns:lock:k"


def test_public_surface_exports_core_symbols() -> None:
    for name in (
        "Cache",
        "CacheConfig",
        "CacheManager",
        "MemoryCache",
        "RedisCache",
        "TieredCache",
        "NullCache",
        "cached",
        "memory_cache",
        "tiered_cache",
        "FakeClock",
        "CacheMetrics",
        "JsonCodec",
        "PickleCodec",
        "build_cache_manager",
    ):
        assert hasattr(cache_pkg, name), f"missing export: {name}"
        assert name in cache_pkg.__all__
