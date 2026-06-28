"""FlagCache tests — TTL freshness + fail-open, no Redis required."""

from __future__ import annotations

import pytest

from app.flags.cache import FlagCache
from app.flags.models import EMPTY_SNAPSHOT, Flag, FlagSnapshot

pytestmark = pytest.mark.asyncio


def _snap(version: int, *keys: str) -> FlagSnapshot:
    return FlagSnapshot.from_flags(tuple(Flag.boolean(k) for k in keys), version=version)


async def test_cold_cache_loads_on_first_get() -> None:
    calls = {"n": 0}

    async def loader() -> FlagSnapshot:
        calls["n"] += 1
        return _snap(1, "x")

    cache = FlagCache(loader, ttl_s=100.0)
    assert cache.current is EMPTY_SNAPSHOT  # not loaded yet
    snap = await cache.get()
    assert snap.version == 1
    assert calls["n"] == 1
    # within TTL -> no reload
    await cache.get()
    assert calls["n"] == 1


async def test_force_reload() -> None:
    versions = iter([1, 2, 3])

    async def loader() -> FlagSnapshot:
        return _snap(next(versions), "x")

    cache = FlagCache(loader, ttl_s=1000.0)
    assert (await cache.get()).version == 1
    assert (await cache.get(force=True)).version == 2


async def test_ttl_expiry_triggers_reload() -> None:
    calls = {"n": 0}

    async def loader() -> FlagSnapshot:
        calls["n"] += 1
        return _snap(calls["n"], "x")

    cache = FlagCache(loader, ttl_s=0.0)  # always stale
    await cache.get()
    await cache.get()
    assert calls["n"] == 2  # reloaded each time


async def test_reload_fails_open_keeps_last_snapshot() -> None:
    state = {"fail": False}

    async def loader() -> FlagSnapshot:
        if state["fail"]:
            raise RuntimeError("db down")
        return _snap(5, "x")

    cache = FlagCache(loader, ttl_s=0.0)
    good = await cache.get()
    assert good.version == 5
    # now the loader breaks; cache must keep serving the last snapshot
    state["fail"] = True
    after = await cache.reload()
    assert after.version == 5


async def test_publish_invalidation_noop_without_redis() -> None:
    cache = FlagCache(lambda: _ret(_snap(1, "x")), ttl_s=10.0)
    assert await cache.publish_invalidation() == 0
    assert await cache.remote_version() is None


async def test_listen_returns_immediately_without_redis() -> None:
    cache = FlagCache(lambda: _ret(_snap(1, "x")))
    seen = [v async for v in cache.listen()]
    assert seen == []


async def _ret(snapshot: FlagSnapshot) -> FlagSnapshot:
    return snapshot
