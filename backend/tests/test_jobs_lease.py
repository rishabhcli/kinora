"""Leader-election lease tests (require an isolated test Redis, e.g. db 15)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio

from app.jobs.clock import ManualClock
from app.jobs.lease import LeaderElector, LeaderLease

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")

requires_redis = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping lease tests"
)

# Generous lease TTL so a slow machine under full-suite load can never expire a
# lease mid-test, and a per-test unique key prefix so the autouse db-15 flush
# (conftest._isolate_state) can't strand or collide on a sibling test's keys.
_TTL_MS = 60_000


@pytest_asyncio.fixture
async def redis_raw() -> AsyncIterator[object]:
    assert _REDIS_URL is not None
    from redis.asyncio import Redis

    # single_connection_client: avoid the shared (URL-keyed) async pool being
    # reused across the per-test event loops the autouse fixtures churn.
    client = Redis.from_url(_REDIS_URL, decode_responses=True, single_connection_client=True)
    try:
        yield client
    finally:
        await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture
def lease_name() -> Callable[[], str]:
    """A fresh, collision-proof lease key for each call within a test."""

    def _name() -> str:
        return f"kinora:test:lease:{uuid.uuid4().hex}"

    return _name


pytestmark = requires_redis


async def test_only_one_contender_acquires(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    a = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    b = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await a.acquire() is True
    assert await b.acquire() is False
    assert a.held
    assert not b.held
    assert a.fence is not None


async def test_release_frees_lease_for_next(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    a = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    b = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await a.acquire()
    assert await a.release()
    assert await b.acquire()


async def test_renew_extends_and_only_owner(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    a = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    b = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await a.acquire()
    fence_before = a.fence
    assert await a.renew() is True
    assert a.fence is not None
    assert fence_before is not None
    # Renew keeps the same fence (no re-acquisition); the token only bumps on a
    # fresh acquire so a stalled-then-resumed old leader is detectable.
    assert a.fence == fence_before
    # b never owned it -> renew fails and reports leadership lost.
    assert await b.renew() is False
    assert not b.held


async def test_release_by_non_owner_is_noop(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    a = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    b = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await a.acquire()
    assert await b.release() is False  # b doesn't own it
    assert await a.renew() is True  # a still holds it


async def test_fence_increments_across_acquisitions(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    a = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await a.acquire()
    f1 = a.fence
    await a.release()
    b = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await b.acquire()
    f2 = b.fence
    assert f1 is not None and f2 is not None
    assert f2 > f1


async def test_elector_tick_acquires_then_renews(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    clock = ManualClock()
    lease = LeaderLease(redis_raw, name=lease_name(), ttl_ms=_TTL_MS)
    elector = LeaderElector(lease, clock=clock, renew_interval_s=2.0)
    assert not elector.is_leader
    assert await elector.tick() is True  # first tick acquires
    assert elector.is_leader
    assert elector.fence is not None
    assert await elector.tick() is True  # subsequent tick renews
    await elector.stop()


async def test_elector_follower_when_leader_held_elsewhere(
    redis_raw: object, lease_name: Callable[[], str]
) -> None:
    name = lease_name()
    other = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    assert await other.acquire()
    clock = ManualClock()
    lease = LeaderLease(redis_raw, name=name, ttl_ms=_TTL_MS)
    elector = LeaderElector(lease, clock=clock)
    assert await elector.tick() is False  # someone else holds it
    assert not elector.is_leader
    assert elector.fence is None
    await elector.stop()
