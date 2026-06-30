"""End-to-end control loop: tick assigns, steals, recovers crashes, reports."""

from __future__ import annotations

from app.orchestration.capacity import CapacityOracle, StaticCapacityOracle
from app.orchestration.clock import VirtualClock
from app.orchestration.models import Lane
from app.orchestration.service import (
    OrchestrationService,
    TicketSource,
    build_orchestration_service,
)

from .conftest import caps, ticket


def _service(
    clock: VirtualClock,
    *,
    oracle: CapacityOracle | None = None,
    ticket_source: TicketSource | None = None,
) -> OrchestrationService:
    return build_orchestration_service(
        clock=clock,
        oracle=oracle or StaticCapacityOracle(max_inflight={"wan": 64, "keyframe": 64}),
        ticket_source=ticket_source,
    )


async def test_tick_assigns_pending_tickets(clock: VirtualClock) -> None:
    svc = _service(clock)
    await svc.registry.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    report = await svc.tick([ticket("s1", provider="wan"), ticket("s2", provider="wan")])
    assert report.assignment.assigned_count == 2
    assert report.progress.total_inflight == 2


async def test_tick_uses_injected_ticket_source(clock: VirtualClock) -> None:
    pending = [ticket("s1", provider="wan")]
    svc = _service(clock, ticket_source=lambda: pending)
    await svc.registry.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    report = await svc.tick()
    assert report.assignment.assigned_count == 1


async def test_tick_skips_already_leased_shots(clock: VirtualClock) -> None:
    svc = _service(clock)
    await svc.registry.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    t = ticket("s1", provider="wan")
    await svc.tick([t])
    # Second tick with the same pending ticket places nothing new (idempotent).
    report = await svc.tick([t])
    assert report.assignment.assigned_count == 0
    assert report.progress.total_inflight == 1


async def test_tick_recovers_crashed_worker_and_rehomes(clock: VirtualClock) -> None:
    svc = _service(clock)
    await svc.registry.register("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    await svc.registry.register("w2", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    t = ticket("s1", book_id="book-1", provider="wan")
    first = await svc.tick([t])
    owner = first.assignment.assigned[0].worker_id
    survivor = "w2" if owner == "w1" else "w1"

    # Time jumps past the worker TTL; the survivor keeps heartbeating.
    clock.advance(100_000)
    await svc.registry.heartbeat(survivor)

    # A tick now: sweep reclaims the orphan, and the same tick re-homes it.
    report = await svc.tick([t])
    assert report.did_recover
    assert owner in report.sweep.dead_workers
    assert report.assignment.assigned_count == 1
    assert report.assignment.assigned[0].worker_id == survivor


async def test_tick_steals_to_balance_idle_worker(clock: VirtualClock) -> None:
    # One worker draws all the work, then a second worker joins; a tick rebalances.
    svc = build_orchestration_service(
        clock=clock,
        oracle=StaticCapacityOracle(max_inflight={"wan": 64}),
    )
    await svc.registry.register("w1", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8))
    # Place 4 speculative shots across distinct books (so locality doesn't pin one).
    tickets = [
        ticket(f"s{i}", book_id=f"book-{i}", lane=Lane.SPECULATIVE, provider="wan")
        for i in range(4)
    ]
    await svc.tick(tickets)
    leases_before = await svc.coordinator.store.list_leases()
    assert {lease.worker_id for lease in leases_before} == {"w1"}

    # A fresh idle worker joins.
    await svc.registry.register("w2", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8))
    report = await svc.tick([])  # nothing new queued — pure rebalance
    assert len(report.stolen) >= 1
    # Work is now spread across both workers.
    by_worker: dict[str, int] = {}
    for m in report.stolen:
        by_worker[m.to_worker] = by_worker.get(m.to_worker, 0) + 1
    assert by_worker.get("w2", 0) >= 1


async def test_stolen_shot_lease_fence_advances(clock: VirtualClock) -> None:
    svc = build_orchestration_service(
        clock=clock, oracle=StaticCapacityOracle(max_inflight={"wan": 64})
    )
    await svc.registry.register("w1", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8))
    tickets = [
        ticket(f"s{i}", book_id=f"book-{i}", lane=Lane.SPECULATIVE, provider="wan")
        for i in range(4)
    ]
    await svc.tick(tickets)
    await svc.registry.register("w2", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8))
    report = await svc.tick([])
    assert report.stolen
    moved = report.stolen[0]
    store = svc.coordinator.store
    lease = await store.get_lease(moved.shot_hash)
    assert lease is not None
    assert lease.worker_id == moved.to_worker  # exactly-once handoff to new worker
    assert lease.fence == 2  # fence advanced — the old worker is fenced out


async def test_settings_drive_orchestration_config(clock: VirtualClock) -> None:
    from app.core.config import Settings

    settings = Settings(
        DASHSCOPE_API_KEY="test",
        orchestration_worker_ttl_ms=4242,
        orchestration_lease_ttl_ms=5151,
        orchestration_rebalance_max_steals=7,
    )
    svc = build_orchestration_service(clock=clock, settings=settings)
    assert svc.registry.config.worker_ttl_ms == 4242
    assert svc.registry.config.lease_ttl_ms == 5151
