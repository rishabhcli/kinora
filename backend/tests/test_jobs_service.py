"""Tests for the JobService facade + build_job_service factory (no infra)."""

from __future__ import annotations

import asyncio

import pytest

from app.jobs.clock import ManualClock
from app.jobs.registry import JobRegistry, job
from app.jobs.service import JobService, build_job_service
from app.jobs.store import InMemoryJobStore
from app.jobs.triggers import every, manual
from app.jobs.types import JobContext, JobResult, JobRunStatus


def _registry() -> tuple[JobRegistry, dict]:
    reg = JobRegistry()
    state: dict = {"runs": 0}

    @job("svc.job", trigger=every(30), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        state["runs"] += 1
        return JobResult.ok()

    @job("svc.manual", trigger=manual(), registry=reg)
    async def manual_handler(ctx: JobContext) -> JobResult:
        state["runs"] += 1
        return JobResult.ok()

    return reg, state


def test_service_single_node_is_always_leader() -> None:
    reg, _ = _registry()
    svc = JobService(registry=reg, store=InMemoryJobStore())
    assert svc.is_leader is True  # no elector => single-node leader


async def test_run_now_enqueues_and_dedups() -> None:
    reg, state = _registry()
    clock = ManualClock()
    svc = JobService(registry=reg, store=InMemoryJobStore(clock=clock), clock=clock)
    first = await svc.run_now("svc.manual")
    second = await svc.run_now("svc.manual")
    assert first is not None
    assert second is None  # same minute -> dedup
    runs = await svc.list_runs(job_name="svc.manual")
    assert len(runs) == 1


async def test_pause_resume_via_service() -> None:
    reg, _ = _registry()
    svc = JobService(registry=reg, store=InMemoryJobStore())
    svc.pause("svc.job")
    assert svc.is_paused("svc.job")
    svc.resume("svc.job")
    assert not svc.is_paused("svc.job")


async def test_stats_and_dead_letters_passthrough() -> None:
    reg, _ = _registry()
    clock = ManualClock()
    svc = JobService(registry=reg, store=InMemoryJobStore(clock=clock), clock=clock)
    await svc.run_now("svc.manual")
    stats = await svc.stats()
    assert stats.enqueued_total == 1
    assert await svc.dead_letters() == []


async def test_replay_creates_new_run_from_terminal() -> None:
    reg = JobRegistry()
    attempts = {"n": 0}

    @job("svc.replayable", trigger=manual(), max_attempts=1, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("first time fails")
        return JobResult.ok()

    clock = ManualClock()
    store = InMemoryJobStore(clock=clock)
    svc = JobService(registry=reg, store=store, clock=clock, seed=0)
    run_id = await svc.run_now("svc.replayable")
    assert run_id is not None
    # Drive the worker once -> dead-letters (max_attempts=1).
    await svc.worker.drain()
    dl = await svc.dead_letters()
    assert len(dl) == 1
    # Replay -> a fresh active run that now succeeds.
    new_id = await svc.replay(dl[0].id)
    assert new_id is not None
    assert new_id != run_id
    await svc.worker.drain()
    replayed = await store.get(new_id)
    assert replayed is not None
    assert replayed.status is JobRunStatus.SUCCEEDED
    assert attempts["n"] == 2


async def test_replay_unknown_run_returns_none() -> None:
    reg, _ = _registry()
    svc = JobService(registry=reg, store=InMemoryJobStore())
    assert await svc.replay("does-not-exist") is None


async def test_start_stop_runs_jobs_in_real_time() -> None:
    """Smoke the actual background loops on the system clock (fast cadence)."""
    reg = JobRegistry()
    fired = asyncio.Event()

    @job("svc.fast", trigger=every(0.05), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        fired.set()
        return JobResult.ok()

    svc = JobService(
        registry=reg,
        store=InMemoryJobStore(),
        scheduler_poll_s=0.02,
        worker_idle_s=0.02,
    )
    svc.start()
    try:
        await asyncio.wait_for(fired.wait(), timeout=3.0)
    finally:
        await svc.stop()
    assert fired.is_set()


async def test_start_is_idempotent_and_stop_safe_before_start() -> None:
    reg, _ = _registry()
    svc = JobService(registry=reg, store=InMemoryJobStore())
    await svc.stop()  # safe before start
    svc.start()
    svc.start()  # idempotent
    await svc.stop()


def test_build_job_service_memory_default() -> None:
    svc = build_job_service(store_backend="memory", enable_leader_election=False)
    assert isinstance(svc.store, InMemoryJobStore)
    assert svc.elector is None


def test_build_job_service_redis_requires_client() -> None:
    with pytest.raises(ValueError, match="redis client"):
        build_job_service(store_backend="redis")


def test_build_job_service_postgres_requires_factory() -> None:
    with pytest.raises(ValueError, match="session_factory"):
        build_job_service(store_backend="postgres")


def test_build_job_service_auto_prefers_postgres_then_redis() -> None:
    # With neither, auto falls back to in-memory.
    svc = build_job_service(store_backend="auto")
    assert isinstance(svc.store, InMemoryJobStore)
