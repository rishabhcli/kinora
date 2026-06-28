"""Tests for the built-in maintenance jobs — no-op-safe + wired paths (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.jobs.harness import VirtualClockHarness
from app.jobs.maintenance import (
    BUDGET_RECONCILER,
    DIGEST_FLUSHER,
    IMPORT_RECOVERY,
    MAINTENANCE_JOB_NAMES,
    RETENTION_GC,
    SEARCH_INDEXER,
    default_maintenance_registry,
    register_maintenance_jobs,
)
from app.jobs.registry import JobRegistry
from app.jobs.types import RunOutcome


def start() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_all_five_jobs_registered() -> None:
    reg = default_maintenance_registry()
    assert set(reg.names()) == set(MAINTENANCE_JOB_NAMES)
    assert len(MAINTENANCE_JOB_NAMES) == 5


def test_register_into_existing_registry_returns_it() -> None:
    reg = JobRegistry()
    same = register_maintenance_jobs(reg)
    assert same is reg
    assert len(reg) == 5


async def test_all_jobs_skip_cleanly_when_unwired() -> None:
    reg = default_maintenance_registry()
    h = VirtualClockHarness(reg, start=start())  # no resources injected
    # Force-run each and confirm a clean skip (terminal success, not failure).
    for name in MAINTENANCE_JOB_NAMES:
        run_id = await h.run_now(name)
        assert run_id is not None
    results = await h.drain_worker()
    assert len(results) == 5
    assert all(r.outcome is RunOutcome.SKIPPED for r in results)


async def test_digest_flush_runs_when_wired() -> None:
    reg = default_maintenance_registry()
    calls = {"n": 0}

    async def flusher() -> dict:
        calls["n"] += 1
        return {"flushed": 12}

    h = VirtualClockHarness(reg, start=start(), resources={DIGEST_FLUSHER: flusher})
    await h.run_now("maintenance.digest_flush")
    results = await h.drain_worker()
    assert calls["n"] == 1
    assert results[0].outcome is RunOutcome.SUCCESS
    run = (await h.store.list_runs(job_name="maintenance.digest_flush"))[0]
    assert run.detail == {"flushed": 12}


async def test_int_return_becomes_processed_detail() -> None:
    reg = default_maintenance_registry()

    async def gc() -> int:
        return 7

    h = VirtualClockHarness(reg, start=start(), resources={RETENTION_GC: gc})
    await h.run_now("maintenance.retention_gc")
    await h.drain_worker()
    run = (await h.store.list_runs(job_name="maintenance.retention_gc"))[0]
    assert run.detail == {"processed": 7}


async def test_non_callable_resource_skips() -> None:
    reg = default_maintenance_registry()
    h = VirtualClockHarness(reg, start=start(), resources={SEARCH_INDEXER: "not-callable"})
    await h.run_now("maintenance.search_index_refresh")
    results = await h.drain_worker()
    assert results[0].outcome is RunOutcome.SKIPPED
    assert "not callable" in str(
        (await h.store.list_runs(job_name="maintenance.search_index_refresh"))[0].detail
    )


async def test_import_recovery_and_budget_reconcile_wired() -> None:
    reg = default_maintenance_registry()
    recovered = {"n": 0}
    reconciled = {"n": 0}

    async def recovery() -> dict:
        recovered["n"] += 1
        return {"respawned": 2}

    async def reconcile() -> dict:
        reconciled["n"] += 1
        return {"adjusted_seconds": 0.0}

    h = VirtualClockHarness(
        reg,
        start=start(),
        resources={IMPORT_RECOVERY: recovery, BUDGET_RECONCILER: reconcile},
    )
    await h.run_now("maintenance.stuck_import_recovery")
    await h.run_now("maintenance.budget_reconcile")
    await h.drain_worker()
    assert recovered["n"] == 1
    assert reconciled["n"] == 1


async def test_wired_resource_that_raises_retries() -> None:
    reg = JobRegistry()
    register_maintenance_jobs(reg)
    attempts = {"n": 0}

    async def flaky() -> dict:
        attempts["n"] += 1
        raise RuntimeError("transient backend error")

    h = VirtualClockHarness(reg, start=start(), resources={DIGEST_FLUSHER: flaky}, seed=0)
    await h.run_now("maintenance.digest_flush")
    # max_attempts=3 for digest_flush -> retries twice then dead-letters.
    for _ in range(5):
        await h.advance(600)
        await h.drain_worker()
    dl = await h.store.dead_letters()
    assert len(dl) == 1
    assert attempts["n"] == 3


async def test_maintenance_jobs_fire_on_cadence() -> None:
    reg = default_maintenance_registry()
    h = VirtualClockHarness(reg, start=start())
    # digest_flush every 60s; advance 60 and the scheduler should enqueue it.
    await h.advance(60)
    enqueued = await h.tick_scheduler()
    assert "maintenance.digest_flush" in {
        (await h.store.get(rid)).job_name for rid in enqueued  # type: ignore[union-attr]
    }
