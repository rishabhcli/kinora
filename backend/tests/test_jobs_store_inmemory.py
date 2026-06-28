"""Unit tests for the in-memory job store (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.jobs.clock import ManualClock
from app.jobs.store import EnqueueResult, InMemoryJobStore
from app.jobs.types import JobRunStatus, RunOutcome, TriggerKind


def at(mi: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, mi, s, tzinfo=UTC)


async def _store() -> InMemoryJobStore:
    return InMemoryJobStore(clock=ManualClock(start=at(0)))


async def _enqueue(
    store: InMemoryJobStore, key: str = "j@k", name: str = "j", **kw: Any
) -> EnqueueResult:
    return await store.enqueue(
        job_name=name,
        idempotency_key=key,
        scheduled_for=kw.get("scheduled_for", at(0)),
        max_attempts=kw.get("max_attempts", 3),
        trigger_kind=kw.get("trigger_kind", TriggerKind.INTERVAL),
        payload=kw.get("payload"),
        available_at=kw.get("available_at"),
    )


async def test_enqueue_creates_pending_run() -> None:
    store = await _store()
    res = await _enqueue(store)
    assert res.created
    assert res.run.status is JobRunStatus.PENDING
    assert res.run.attempt == 0


async def test_enqueue_dedups_on_active_key() -> None:
    store = await _store()
    first = await _enqueue(store, key="dup")
    second = await _enqueue(store, key="dup")
    assert first.created
    assert not second.created
    assert second.run.id == first.run.id
    stats = await store.stats()
    assert stats.enqueued_total == 1


async def test_enqueue_after_terminal_creates_new_run() -> None:
    store = await _store()
    first = await _enqueue(store, key="cycle")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.complete(claimed.id, outcome=RunOutcome.SUCCESS, detail={})
    # Key freed -> a fresh enqueue creates a new run.
    second = await _enqueue(store, key="cycle")
    assert second.created
    assert second.run.id != first.run.id


async def test_claim_respects_available_at() -> None:
    store = await _store()
    await _enqueue(store, key="future", available_at=at(5))
    assert await store.claim_due(now=at(0), lease_seconds=60) is None
    claimed = await store.claim_due(now=at(5), lease_seconds=60)
    assert claimed is not None
    assert claimed.status is JobRunStatus.RUNNING
    assert claimed.attempt == 1
    assert claimed.lease_token is not None


async def test_claim_is_exclusive() -> None:
    store = await _store()
    await _enqueue(store, key="solo")
    a = await store.claim_due(now=at(0), lease_seconds=60)
    b = await store.claim_due(now=at(0), lease_seconds=60)
    assert a is not None
    assert b is None  # already leased


async def test_claim_orders_by_availability() -> None:
    store = await _store()
    await _enqueue(store, key="later", name="later", available_at=at(2))
    await _enqueue(store, key="earlier", name="earlier", available_at=at(1))
    claimed = await store.claim_due(now=at(5), lease_seconds=60)
    assert claimed is not None
    assert claimed.job_name == "earlier"


async def test_claim_can_filter_job_names() -> None:
    store = await _store()
    await _enqueue(store, key="a", name="alpha")
    await _enqueue(store, key="b", name="beta")
    claimed = await store.claim_due(now=at(0), lease_seconds=60, job_names=["beta"])
    assert claimed is not None
    assert claimed.job_name == "beta"


async def test_retry_reschedules_and_keeps_key_active() -> None:
    store = await _store()
    await _enqueue(store, key="retryme")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.retry(claimed.id, available_at=at(0, 8), error="boom")
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.RETRYING
    assert run.error == "boom"
    # Not yet available -> not claimable.
    assert await store.claim_due(now=at(0, 2), lease_seconds=60) is None
    # After backoff -> claimable again, attempt increments.
    again = await store.claim_due(now=at(0, 8), lease_seconds=60)
    assert again is not None
    assert again.attempt == 2


async def test_deadletter_terminal() -> None:
    store = await _store()
    await _enqueue(store, key="dead")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.deadletter(claimed.id, error="fatal")
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.DEADLETTER
    assert run.is_terminal
    dl = await store.dead_letters()
    assert len(dl) == 1
    stats = await store.stats()
    assert stats.deadletter_total == 1
    assert stats.dead_letters == 1


async def test_cancel_pending_run() -> None:
    store = await _store()
    res = await _enqueue(store, key="cancelme")
    assert await store.cancel(res.run.id) is True
    run = await store.get(res.run.id)
    assert run is not None
    assert run.status is JobRunStatus.CANCELLED
    # Cancelling a terminal run is a no-op.
    assert await store.cancel(res.run.id) is False


async def test_reap_expired_requeues_lapsed_lease() -> None:
    store = await _store()
    await _enqueue(store, key="lease")
    claimed = await store.claim_due(now=at(0), lease_seconds=30)
    assert claimed is not None
    # Lease not yet expired.
    assert await store.reap_expired(now=at(0, 20)) == 0
    # Lease expired -> reaped back to retrying.
    reaped = await store.reap_expired(now=at(1))
    assert reaped == 1
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.RETRYING


async def test_skipped_outcome_is_terminal_success() -> None:
    store = await _store()
    await _enqueue(store, key="skip")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.complete(claimed.id, outcome=RunOutcome.SKIPPED, detail={"reason": "nothing"})
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.SKIPPED
    assert run.is_terminal


async def test_list_runs_filters_and_orders() -> None:
    store = await _store()
    await _enqueue(store, key="x1", name="x")
    await _enqueue(store, key="y1", name="y")
    xs = await store.list_runs(job_name="x")
    assert len(xs) == 1
    assert xs[0].job_name == "x"
    pendings = await store.list_runs(status=JobRunStatus.PENDING)
    assert len(pendings) == 2


async def test_stats_active_count() -> None:
    store = await _store()
    await _enqueue(store, key="a1")
    await _enqueue(store, key="a2")
    stats = await store.stats()
    assert stats.active == 2


async def test_get_returns_copy_not_internal_reference() -> None:
    store = await _store()
    res = await _enqueue(store, key="copy")
    snapshot = await store.get(res.run.id)
    assert snapshot is not None
    snapshot.status = JobRunStatus.FAILED  # mutate the copy
    fresh = await store.get(res.run.id)
    assert fresh is not None
    assert fresh.status is JobRunStatus.PENDING  # store unaffected
