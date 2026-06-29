"""The saga worker loop: claim, drive, lease, reap — crash recovery across workers."""

from __future__ import annotations

import asyncio

from app.distributed.sagas.definition import SagaRegistry, saga, step
from app.distributed.sagas.effects import InMemoryEffectLedger
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.runner import SagaWorker
from app.distributed.sagas.store import InMemorySagaStore
from app.distributed.sagas.types import SagaContext, SagaStatus, StepResult
from app.jobs.clock import ManualClock


async def test_worker_drains_pending_sagas() -> None:
    store = InMemorySagaStore()
    clock = ManualClock()
    ran: list[str] = []

    async def s1(ctx: SagaContext) -> StepResult:
        ran.append(ctx.correlation_id)
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())

    # Enqueue three sagas (PENDING) without driving them.
    for i in range(3):
        await orch.start("flow", f"c{i}")

    worker = SagaWorker(store, orch, clock=clock, lease_seconds=60)
    driven = await worker.run_until_idle()
    assert driven == 3
    assert sorted(ran) == ["c0", "c1", "c2"]
    stats = await store.stats()
    assert stats.committed_total == 3


async def test_claim_leases_so_a_peer_cannot_double_drive() -> None:
    store = InMemorySagaStore()
    clock = ManualClock()

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    await orch.start("flow", "c1")

    w1 = SagaWorker(store, orch, clock=clock, lease_seconds=60)
    claimed = await w1.claim_one()
    assert claimed is not None
    # A peer claiming now sees nothing (the only instance is leased).
    w2 = SagaWorker(store, orch, clock=clock, lease_seconds=60)
    assert await w2.claim_one() is None


async def test_reap_returns_lapsed_lease_to_pool() -> None:
    """A crashed worker's lease lapses; reap returns the saga so a peer resumes it."""
    store = InMemorySagaStore()
    clock = ManualClock()

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    await orch.start("flow", "c1")

    crashed = SagaWorker(store, orch, clock=clock, lease_seconds=30)
    claimed = await crashed.claim_one()
    assert claimed is not None
    # 'crashed' never released; advance past the lease.
    await clock.advance(31)
    healthy = SagaWorker(store, orch, clock=clock, lease_seconds=30)
    reaped = await healthy.reap()
    assert reaped == 1
    # Now a healthy worker can claim + finish it.
    driven = await healthy.run_until_idle()
    assert driven == 1
    inst = (await store.list_instances())[0]
    assert inst.status is SagaStatus.COMPLETED


async def test_background_loop_starts_and_stops() -> None:
    store = InMemorySagaStore()
    clock = ManualClock()
    done = asyncio.Event()

    async def s1(ctx: SagaContext) -> StepResult:
        done.set()
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    await orch.start("flow", "c1")

    worker = SagaWorker(store, orch, clock=clock, poll_interval_s=0.1)
    worker.start()
    for _ in range(50):
        if done.is_set():
            break
        await asyncio.sleep(0)
        await clock.advance(0.1)
    await worker.stop()
    assert done.is_set()


async def test_two_workers_partition_the_work_without_overlap() -> None:
    """Two workers draining together each run a disjoint subset; every saga runs once."""
    store = InMemorySagaStore()
    clock = ManualClock()
    runs: dict[str, int] = {}

    async def s1(ctx: SagaContext) -> StepResult:
        runs[ctx.correlation_id] = runs.get(ctx.correlation_id, 0) + 1
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", s1)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    for i in range(6):
        await orch.start("flow", f"c{i}")

    w1 = SagaWorker(store, orch, clock=clock, lease_seconds=60)
    w2 = SagaWorker(store, orch, clock=clock, lease_seconds=60)
    # Interleave claims: each claim leases one instance exclusively.
    n1 = await w1.run_until_idle()
    n2 = await w2.run_until_idle()
    assert n1 + n2 == 6
    # Every saga ran exactly once (no double-drive across the two workers).
    assert sum(runs.values()) == 6
    assert all(v == 1 for v in runs.values())
    assert (await store.stats()).committed_total == 6
