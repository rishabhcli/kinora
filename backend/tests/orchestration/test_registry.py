"""Worker registry: register / heartbeat / drain + dead-worker sweep + reclaim."""

from __future__ import annotations

from app.orchestration.clock import VirtualClock
from app.orchestration.models import Lane, WorkerStatus
from app.orchestration.registry import RegistryConfig, WorkerRegistry
from app.orchestration.store import InMemoryOrchestrationStore

from .conftest import caps


def _registry(store: InMemoryOrchestrationStore, clock: VirtualClock) -> WorkerRegistry:
    return WorkerRegistry(
        store, clock=clock, config=RegistryConfig(worker_ttl_ms=1000, lease_ttl_ms=1000)
    )


async def test_register_then_live(store: InMemoryOrchestrationStore, clock: VirtualClock) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",)))
    live = await reg.live_workers()
    assert [w.worker_id for w in live] == ["w1"]
    assert live[0].status is WorkerStatus.ACTIVE


async def test_heartbeat_keeps_worker_live(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED))
    clock.advance(800)
    await reg.heartbeat("w1")  # refresh before TTL
    clock.advance(800)  # 1600 total since register, but only 800 since heartbeat
    assert [w.worker_id for w in await reg.live_workers()] == ["w1"]


async def test_lapsed_heartbeat_becomes_dead_on_sweep(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED))
    clock.advance(2000)  # past the 1000ms TTL
    report = await reg.sweep()
    assert report.dead_workers == ("w1",)
    assert await reg.live_workers() == []
    worker = await store.get_worker("w1")
    assert worker is not None and worker.status is WorkerStatus.DEAD


async def test_sweep_reclaims_dead_worker_leases(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",)))
    # w1 holds a long lease, but then stops heartbeating.
    await store.try_acquire(
        shot_hash="shot-1", worker_id="w1", lane=Lane.COMMITTED, provider="wan",
        book_id="b", now_ms=clock.now_ms(), ttl_ms=100_000,
    )
    clock.advance(2000)  # worker TTL lapses (lease itself is still 'live')
    report = await reg.sweep()
    assert "w1" in report.dead_workers
    assert [lease.shot_hash for lease in report.reclaimed_leases] == ["shot-1"]
    # The shot is now free for reassignment.
    assert await store.get_lease("shot-1") is None


async def test_sweep_reclaims_expired_lease_even_if_owner_alive(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED, providers=("wan",)))
    await store.try_acquire(
        shot_hash="shot-1", worker_id="w1", lane=Lane.COMMITTED, provider="wan",
        book_id="b", now_ms=clock.now_ms(), ttl_ms=500,  # short lease (missed heartbeat)
    )
    clock.advance(600)  # lease expired; worker heartbeats so it stays alive
    await reg.heartbeat("w1")
    report = await reg.sweep()
    assert report.dead_workers == ()  # owner is alive
    assert [lease.shot_hash for lease in report.reclaimed_leases] == ["shot-1"]


async def test_drain_excludes_from_assignable_but_stays_live(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED))
    await reg.drain("w1")
    assert [w.worker_id for w in await reg.live_workers()] == ["w1"]  # still live
    assert await reg.assignable_workers() == []  # but not offered new work


async def test_dead_worker_heartbeat_resurrects(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    await reg.register("w1", caps(Lane.COMMITTED))
    clock.advance(2000)
    await reg.sweep()  # -> DEAD
    refreshed = await reg.heartbeat("w1")
    assert refreshed is not None and refreshed.status is WorkerStatus.ACTIVE


async def test_heartbeat_unknown_worker_returns_none(
    store: InMemoryOrchestrationStore, clock: VirtualClock
) -> None:
    reg = _registry(store, clock)
    assert await reg.heartbeat("ghost") is None
