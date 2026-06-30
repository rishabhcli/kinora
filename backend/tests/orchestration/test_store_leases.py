"""Lease acquire / expire / reassign + fence (single-renderer) — store level."""

from __future__ import annotations

import asyncio

import pytest

from app.orchestration.clock import VirtualClock
from app.orchestration.models import FenceViolationError, Lane, ShotLease
from app.orchestration.store import InMemoryOrchestrationStore


async def _acquire(
    store: InMemoryOrchestrationStore, worker: str, *, now_ms: int, ttl_ms: int
) -> ShotLease | None:
    return await store.try_acquire(
        shot_hash="shot-1",
        worker_id=worker,
        lane=Lane.COMMITTED,
        provider="wan",
        book_id="book-1",
        now_ms=now_ms,
        ttl_ms=ttl_ms,
    )


async def test_acquire_grants_lease_with_fence_one(store: InMemoryOrchestrationStore) -> None:
    lease = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    assert lease is not None
    assert lease.worker_id == "w1"
    assert lease.fence == 1
    assert lease.expires_at_ms == 1000
    assert lease.held_by("w1")


async def test_second_acquire_while_live_is_denied(store: InMemoryOrchestrationStore) -> None:
    first = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    second = await _acquire(store, "w2", now_ms=500, ttl_ms=1000)
    assert first is not None
    # Single-renderer invariant: a live lease blocks every other worker.
    assert second is None
    held = await store.get_lease("shot-1")
    assert held is not None and held.worker_id == "w1"


async def test_expired_lease_is_reacquirable_with_advanced_fence(
    store: InMemoryOrchestrationStore,
) -> None:
    first = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    assert first is not None and first.fence == 1
    # After expiry, a different worker may take it; the fence advances.
    second = await _acquire(store, "w2", now_ms=1500, ttl_ms=1000)
    assert second is not None
    assert second.worker_id == "w2"
    assert second.fence == 2  # strictly greater than the reclaimed lease's fence


async def test_stale_fence_extend_is_fenced_out(store: InMemoryOrchestrationStore) -> None:
    stale = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    assert stale is not None
    # w2 takes over after expiry (fence advances to 2).
    await _acquire(store, "w2", now_ms=1500, ttl_ms=1000)
    # The zombie w1 wakes up and tries to heartbeat with its old fence -> rejected.
    with pytest.raises(FenceViolationError):
        await store.extend(shot_hash="shot-1", fence=stale.fence, now_ms=1600, ttl_ms=1000)


async def test_stale_fence_release_is_fenced_out(store: InMemoryOrchestrationStore) -> None:
    stale = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    assert stale is not None
    await _acquire(store, "w2", now_ms=1500, ttl_ms=1000)
    with pytest.raises(FenceViolationError):
        await store.release(shot_hash="shot-1", fence=stale.fence)
    # The new owner can still release with its own fence.
    current = await store.get_lease("shot-1")
    assert current is not None
    assert await store.release(shot_hash="shot-1", fence=current.fence) is True


async def test_extend_pushes_expiry_and_keeps_fence(store: InMemoryOrchestrationStore) -> None:
    lease = await _acquire(store, "w1", now_ms=0, ttl_ms=1000)
    assert lease is not None
    extended = await store.extend(shot_hash="shot-1", fence=lease.fence, now_ms=900, ttl_ms=1000)
    assert extended.fence == lease.fence  # same holder, same fence
    assert extended.expires_at_ms == 1900
    # The pushed-out lease is NOT expired at a time the original would have been.
    assert not extended.is_expired(now_ms=1500)


async def test_reap_expired_removes_only_lapsed_leases(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    await store.try_acquire(
        shot_hash="a", worker_id="w1", lane=Lane.COMMITTED, provider="wan",
        book_id="b", now_ms=0, ttl_ms=1000,
    )
    await store.try_acquire(
        shot_hash="b", worker_id="w1", lane=Lane.COMMITTED, provider="wan",
        book_id="b", now_ms=0, ttl_ms=5000,
    )
    reaped = await store.reap_expired(now_ms=2000)
    assert {lease.shot_hash for lease in reaped} == {"a"}
    assert await store.get_lease("a") is None
    assert await store.get_lease("b") is not None


async def test_single_renderer_under_contention(store: InMemoryOrchestrationStore) -> None:
    """Many workers race for one free shot; the store grants exactly one lease."""
    async def grab(worker: str) -> ShotLease | None:
        return await _acquire(store, worker, now_ms=0, ttl_ms=10_000)

    results = await asyncio.gather(*(grab(f"w{i}") for i in range(50)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # exactly-once: a single worker wins the lease
    # And the store agrees on who.
    held = await store.get_lease("shot-1")
    assert held is not None and held.worker_id == winners[0].worker_id
