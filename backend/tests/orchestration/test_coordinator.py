"""Coordinator: capability-matched assignment, locality, exactly-once, recovery."""

from __future__ import annotations

import asyncio

import pytest

from app.orchestration.capacity import CapacityOracle, StaticCapacityOracle
from app.orchestration.clock import VirtualClock
from app.orchestration.coordinator import RenderCoordinator
from app.orchestration.models import FenceViolationError, Lane
from app.orchestration.registry import RegistryConfig, WorkerRegistry
from app.orchestration.store import InMemoryOrchestrationStore

from .conftest import caps, ticket


def _build(
    store: InMemoryOrchestrationStore,
    clock: VirtualClock,
    *,
    oracle: CapacityOracle | None = None,
) -> tuple[WorkerRegistry, RenderCoordinator]:
    reg = WorkerRegistry(
        store, clock=clock, config=RegistryConfig(worker_ttl_ms=10_000, lease_ttl_ms=5_000)
    )
    coord = RenderCoordinator(reg, store, clock=clock, oracle=oracle, lease_ttl_ms=5_000)
    return reg, coord


async def test_assigns_only_to_capable_worker(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 4}))
    await reg.register("kf", caps(Lane.KEYFRAME, providers=("keyframe",)))
    await reg.register("vid", caps(Lane.COMMITTED, providers=("wan",)))

    batch = await coord.assign([ticket("s1", lane=Lane.COMMITTED, provider="wan")])
    assert batch.assigned_count == 1
    assert batch.assigned[0].worker_id == "vid"  # only the video worker is capable


async def test_unplaceable_ticket_is_deferred(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 4}))
    await reg.register("kf", caps(Lane.KEYFRAME, providers=("keyframe",)))
    batch = await coord.assign([ticket("s1", lane=Lane.COMMITTED, provider="wan")])
    assert batch.assigned == ()
    assert [t.shot_hash for t in batch.deferred] == ["s1"]
    # No lease was created for an unplaced shot.
    assert await store.get_lease("s1") is None


async def test_committed_placed_before_speculative_when_slots_scarce(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 4}))
    # One worker, one slot.
    both = caps(Lane.COMMITTED, Lane.SPECULATIVE, providers=("wan",), max_concurrency=1)
    await reg.register("w1", both)
    batch = await coord.assign(
        [
            ticket("spec", lane=Lane.SPECULATIVE, provider="wan"),
            ticket("commit", lane=Lane.COMMITTED, provider="wan"),
        ]
    )
    assert batch.assigned_count == 1
    assert batch.assigned[0].ticket.shot_hash == "commit"  # buffer is sacred
    assert [t.shot_hash for t in batch.deferred] == ["spec"]


async def test_book_locality_keeps_shots_on_one_worker(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 16}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8))
    await reg.register("w2", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8))

    # First shot of book-1 establishes the owner.
    first = await coord.assign([ticket("s1", book_id="book-1", provider="wan")])
    owner = first.assigned[0].worker_id
    # Subsequent shots of the same book follow the owner.
    for i in range(2, 6):
        b = await coord.assign([ticket(f"s{i}", book_id="book-1", provider="wan")])
        assert b.assigned[0].worker_id == owner
    leases = await store.list_leases()
    assert {lease.worker_id for lease in leases} == {owner}  # all on one worker


async def test_distinct_books_can_spread_across_workers(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 16}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    await reg.register("w2", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    batch = await coord.assign(
        [
            ticket("a1", book_id="book-A", provider="wan"),
            ticket("b1", book_id="book-B", provider="wan"),
        ]
    )
    workers = {a.ticket.book_id: a.worker_id for a in batch.assigned}
    # Two idle workers, two books -> they land on different workers (load-spread).
    assert workers["book-A"] != workers["book-B"]


async def test_assign_is_idempotent_for_already_leased_shot(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 4}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    t = ticket("s1", provider="wan")
    first = await coord.assign([t])
    assert first.assigned_count == 1
    # Re-assigning the same shot finds it leased and defers (no second lease).
    second = await coord.assign([t])
    assert second.assigned == ()
    assert [d.shot_hash for d in second.deferred] == ["s1"]
    leases = [lease for lease in await store.list_leases() if lease.shot_hash == "s1"]
    assert len(leases) == 1  # exactly one renderer


async def test_exactly_once_under_concurrent_assign(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    """Many coordinators assigning the same shot concurrently -> one lease."""
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 64}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=64))
    t = ticket("hot", provider="wan")
    batches = await asyncio.gather(*(coord.assign([t]) for _ in range(30)))
    total_assigned = sum(b.assigned_count for b in batches)
    assert total_assigned == 1  # the shot is rendered by exactly one worker
    leases = [lease for lease in await store.list_leases() if lease.shot_hash == "hot"]
    assert len(leases) == 1


async def test_heartbeat_then_complete_releases_capacity(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    oracle = StaticCapacityOracle(max_inflight={"wan": 4})
    reg, coord = _build(store, clock, oracle=oracle)
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    batch = await coord.assign([ticket("s1", provider="wan", video_seconds=5.0)])
    lease = batch.assigned[0].lease
    assert oracle.capacity_for("wan").inflight == 1

    clock.advance(1000)  # render is still going; heartbeat pushes the expiry out
    extended = await coord.heartbeat_lease(lease)
    assert extended.expires_at_ms > lease.expires_at_ms

    assert await coord.complete(lease, video_seconds=5.0) is True
    assert oracle.capacity_for("wan").inflight == 0  # headroom returned
    assert await store.get_lease("s1") is None


async def test_completing_with_stale_fence_is_fenced_out(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 4}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    batch = await coord.assign([ticket("s1", provider="wan")])
    stale_lease = batch.assigned[0].lease
    # Expire + reassign so the fence advances.
    clock.advance(6000)
    await coord.assign([ticket("s1", provider="wan")])
    with pytest.raises(FenceViolationError):
        await coord.heartbeat_lease(stale_lease)


async def test_crash_reassignment_rehomes_orphan(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg, coord = _build(store, clock, oracle=StaticCapacityOracle(max_inflight={"wan": 8}))
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    await reg.register("w2", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    t = ticket("s1", book_id="book-1", provider="wan")
    first = await coord.assign([t])
    crashed = first.assigned[0].worker_id

    # The owning worker stops heartbeating; sweep reclaims its lease.
    clock.advance(11_000)
    sweep = await reg.sweep()
    assert crashed in sweep.dead_workers
    assert [lease.shot_hash for lease in sweep.reclaimed_leases] == ["s1"]

    # Re-home onto the surviving worker.
    survivor = "w2" if crashed == "w1" else "w1"
    await reg.heartbeat(survivor)  # keep it alive after the time jump
    rehome = await coord.reassign_orphans([t])
    assert rehome.assigned_count == 1
    assert rehome.assigned[0].worker_id == survivor
    lease = await store.get_lease("s1")
    assert lease is not None and lease.worker_id == survivor
    assert lease.fence == 2  # fence advanced — the crashed worker is fenced out
