"""Stampede protection — single-flight + probabilistic early expiry.

Verifies that concurrent loads of one key collapse into a single loader call and
that early expiry spreads refreshes out. Deterministic: the loader counts calls
and a barrier ensures the concurrent callers genuinely overlap.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.cache.cache import Cache, CacheConfig
from app.cache.clock import FakeClock
from app.cache.errors import SingleFlightError
from app.cache.memory import MemoryCache
from app.cache.metrics import CacheMetrics
from app.cache.singleflight import SingleFlight

pytestmark = pytest.mark.asyncio


def _cache(clk: FakeClock, *, config: CacheConfig | None = None) -> Cache[Any]:
    m = CacheMetrics()
    backend = MemoryCache(clock=clk, metrics=m, metrics_namespace="ns")
    return Cache(backend, namespace="ns", config=config, clock=clk, metrics=m)


async def test_singleflight_collapses_concurrent_loads() -> None:
    sf: SingleFlight[int] = SingleFlight()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def loader() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 99

    # Launch the leader, wait until it's mid-flight, then pile on followers.
    leader = asyncio.create_task(sf.do("k", loader))
    await started.wait()
    followers = [asyncio.create_task(sf.do("k", loader)) for _ in range(5)]
    assert sf.in_flight() == 1
    release.set()
    results = await asyncio.gather(leader, *followers)
    assert results == [99] * 6
    assert calls == 1  # exactly one execution shared by all six callers


async def test_singleflight_clears_after_completion() -> None:
    sf: SingleFlight[int] = SingleFlight()
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await sf.do("k", loader) == 1
    assert sf.in_flight() == 0
    # The slot is freed, so a later call re-runs the loader.
    assert await sf.do("k", loader) == 2


async def test_singleflight_shares_leader_error() -> None:
    sf: SingleFlight[int] = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()

    async def boom() -> int:
        started.set()
        await release.wait()
        raise RuntimeError("leader failed")

    leader = asyncio.create_task(sf.do("k", boom))
    await started.wait()
    follower = asyncio.create_task(sf.do("k", boom))
    release.set()
    # The leader sees the raw error; the follower sees it wrapped.
    with pytest.raises(RuntimeError):
        await leader
    with pytest.raises(SingleFlightError):
        await follower


async def test_singleflight_follower_cancel_does_not_kill_leader() -> None:
    sf: SingleFlight[int] = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 7

    leader = asyncio.create_task(sf.do("k", loader))
    await started.wait()
    follower = asyncio.create_task(sf.do("k", loader))
    await asyncio.sleep(0)  # let the follower attach
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    release.set()
    assert await leader == 7
    assert calls == 1


async def test_cache_get_or_load_is_single_flighted() -> None:
    clk = FakeClock()
    c = _cache(clk)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def loader() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 5

    leader = asyncio.create_task(c.get_or_load("k", loader))
    await started.wait()
    followers = [asyncio.create_task(c.get_or_load("k", loader)) for _ in range(4)]
    release.set()
    results = await asyncio.gather(leader, *followers)
    assert results == [5] * 5
    assert calls == 1  # the herd was collapsed into one load


async def test_early_expiry_triggers_refresh_before_hard_deadline() -> None:
    # With a huge beta the entry will almost certainly volunteer for an early
    # refresh well before its TTL, bumping the loader call count + the metric.
    clk = FakeClock()
    c = _cache(
        clk,
        config=CacheConfig(namespace="ns", default_ttl=100.0, early_expiry_beta=1e6),
    )
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await c.get_or_load("k", loader) == 1
    clk.advance(99.0)  # still inside the TTL, but deep into the early-expiry zone
    await c.get_or_load("k", loader)
    assert calls == 2
    assert c.stats().early_expirations >= 1


async def test_early_expiry_disabled_when_beta_zero() -> None:
    clk = FakeClock()
    c = _cache(
        clk,
        config=CacheConfig(namespace="ns", default_ttl=100.0, early_expiry_beta=0.0),
    )
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await c.get_or_load("k", loader) == 1
    clk.advance(99.0)
    assert await c.get_or_load("k", loader) == 1  # served from cache, no refresh
    assert calls == 1
    assert c.stats().early_expirations == 0


class _FakeLock:
    """A toy async-context lock that records acquisition order."""

    _held: set[str] = set()

    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    async def __aenter__(self) -> _FakeLock:
        self._log.append(f"acquire:{self._name}")
        _FakeLock._held.add(self._name)
        return self

    async def __aexit__(self, *exc: object) -> None:
        _FakeLock._held.discard(self._name)
        self._log.append(f"release:{self._name}")


async def test_cross_process_lock_is_used_for_loads() -> None:
    clk = FakeClock()
    m = CacheMetrics()
    backend = MemoryCache(clock=clk, metrics=m, metrics_namespace="ns")
    log: list[str] = []
    c: Cache[Any] = Cache(
        backend,
        namespace="ns",
        clock=clk,
        metrics=m,
        lock_factory=lambda name: _FakeLock(name, log),
    )

    async def loader() -> int:
        return 1

    assert await c.get_or_load("k", loader) == 1
    # The loader was wrapped in a lock acquire/release.
    assert any(s.startswith("acquire:") for s in log)
    assert any(s.startswith("release:") for s in log)
