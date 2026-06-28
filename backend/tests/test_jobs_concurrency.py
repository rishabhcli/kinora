"""Concurrency / exclusion / failover depth tests (no infra).

Prove the framework's distributed guarantees on the in-memory store (whose
``asyncio.Lock`` models the atomic claim): parallel workers never double-run a
run, idempotent enqueue collapses a thundering herd, and a leader handoff stops
the old leader from scheduling.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

from app.jobs.clock import ManualClock
from app.jobs.dispatcher import Dispatcher
from app.jobs.registry import JobRegistry, job
from app.jobs.runner import JobWorker
from app.jobs.scheduler import JobScheduler
from app.jobs.store import InMemoryJobStore
from app.jobs.triggers import every, manual
from app.jobs.types import JobContext, JobResult, JobRunStatus, TriggerKind


def at(s: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0, s, tzinfo=UTC)


async def test_parallel_workers_never_double_run_a_run() -> None:
    reg = JobRegistry()
    executions: list[str] = []

    @job("exclusive", trigger=manual(), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        executions.append(ctx.run.id)
        await asyncio.sleep(0)  # yield so a sibling worker could interleave
        return JobResult.ok()

    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    dispatcher = Dispatcher(registry=reg, store=store, clock=clock)

    # Enqueue 20 distinct runs.
    for i in range(20):
        await store.enqueue(
            job_name="exclusive",
            idempotency_key=f"exclusive@{i}",
            scheduled_for=at(0),
            max_attempts=1,
            trigger_kind=TriggerKind.MANUAL,
            available_at=at(0),
        )

    # Eight workers drain concurrently.
    workers = [
        JobWorker(store=store, dispatcher=dispatcher, clock=clock, lease_seconds=60)
        for _ in range(8)
    ]
    await asyncio.gather(*(w.drain() for w in workers))

    # Every run executed exactly once (no duplicates despite 8 racing workers).
    assert len(executions) == 20
    assert len(set(executions)) == 20
    succeeded = await store.list_runs(status=JobRunStatus.SUCCEEDED)
    assert len(succeeded) == 20


async def test_idempotent_enqueue_collapses_thundering_herd() -> None:
    store = InMemoryJobStore(clock=ManualClock(start=at(0)))

    async def enqueue_once() -> bool:
        result = await store.enqueue(
            job_name="herd",
            idempotency_key="herd@same-instant",
            scheduled_for=at(0),
            max_attempts=1,
            trigger_kind=TriggerKind.CRON,
        )
        return result.created

    # 50 concurrent enqueues of the same logical run -> exactly one is created.
    results = await asyncio.gather(*(enqueue_once() for _ in range(50)))
    assert sum(results) == 1
    runs = await store.list_runs(job_name="herd")
    assert len(runs) == 1


async def test_two_schedulers_one_run_per_fire() -> None:
    reg = JobRegistry()

    @job("shared.cron", trigger=every(30), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    # Two leader schedulers over the same store (simulating a split-brain blip).
    s1 = JobScheduler(registry=reg, store=store, clock=clock, is_leader=lambda: True)
    s2 = JobScheduler(registry=reg, store=store, clock=clock, is_leader=lambda: True)
    await clock.advance(30)
    r1 = await s1.tick()
    r2 = await s2.tick()
    # Both observe the due fire, but the store dedups -> exactly one run total.
    assert len(r1) + len(r2) == 1
    runs = await store.list_runs(job_name="shared.cron")
    assert len(runs) == 1


async def test_leader_handoff_stops_old_scheduler() -> None:
    reg = JobRegistry()

    # 60s interval so the default minute-granularity idempotency key distinguishes
    # consecutive fires (a sub-minute interval would dedup within one minute).
    @job("handoff", trigger=every(60), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    leadership = {"node_a": True, "node_b": False}
    sa = JobScheduler(
        registry=reg, store=store, clock=clock, is_leader=lambda: leadership["node_a"]
    )
    sb = JobScheduler(
        registry=reg, store=store, clock=clock, is_leader=lambda: leadership["node_b"]
    )

    await clock.advance(60)  # t=60s
    assert len(await sa.tick()) == 1  # A is leader, fires the t=60 run
    assert await sb.tick() == []  # B is follower, no-op

    # Handoff: A loses leadership, B gains it.
    leadership["node_a"] = False
    leadership["node_b"] = True
    await clock.advance(60)  # now t=120s
    assert await sa.tick() == []  # A no longer schedules anything
    # B, newly leader, re-derives from its own epoch and catches up: the t=60 fire
    # is already present (dedup), then it advances and the t=120 fire IS new.
    fired_b: list[str] = []
    for _ in range(3):
        fired_b.extend(await sb.tick())
    assert len(fired_b) == 1  # exactly the t=120 run is newly enqueued by B
    # Total distinct runs across the handoff: t=60 (by A) + t=120 (by B) = 2.
    runs = await store.list_runs(job_name="handoff")
    assert len(runs) == 2


async def test_concurrent_drain_with_retries_is_consistent() -> None:
    reg = JobRegistry()
    attempts: dict[str, int] = {}

    @job("retryrace", trigger=manual(), max_attempts=3, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        attempts[ctx.run.id] = attempts.get(ctx.run.id, 0) + 1
        if attempts[ctx.run.id] < 2:
            raise RuntimeError("flaky once")
        return JobResult.ok()

    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    dispatcher = Dispatcher(registry=reg, store=store, clock=clock, rng=random.Random(0))

    for i in range(10):
        await store.enqueue(
            job_name="retryrace",
            idempotency_key=f"retryrace@{i}",
            scheduled_for=at(0),
            max_attempts=3,
            trigger_kind=TriggerKind.MANUAL,
            available_at=at(0),
        )

    workers = [
        JobWorker(store=store, dispatcher=dispatcher, clock=clock, lease_seconds=60)
        for _ in range(4)
    ]
    # First pass: each fails once -> retries scheduled with backoff.
    await asyncio.gather(*(w.drain() for w in workers))
    # Advance past backoff and drain again -> all succeed on attempt 2.
    await clock.advance(300)
    await asyncio.gather(*(w.drain() for w in workers))

    succeeded = await store.list_runs(status=JobRunStatus.SUCCEEDED)
    assert len(succeeded) == 10
    assert all(v == 2 for v in attempts.values())
