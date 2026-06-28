"""Postgres durable store tests (require the isolated jobs test DB).

Re-run the store contract against the real Postgres implementation and prove the
partial-unique-index dedup + ``FOR UPDATE SKIP LOCKED`` claim hold under the ORM.
The conftest ``_isolate_state`` fixture ensures the schema (via ``create_all``,
which now includes ``job_runs`` / ``scheduled_jobs``) and truncates between tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.composition import make_session_factory
from app.jobs.db_store import PostgresJobStore
from app.jobs.harness import VirtualClockHarness
from app.jobs.registry import JobRegistry, job
from app.jobs.store import EnqueueResult
from app.jobs.triggers import every
from app.jobs.types import JobContext, JobResult, JobRunStatus, RunOutcome, TriggerKind

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping postgres store tests"
)


def at(mi: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, mi, s, tzinfo=UTC)


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PostgresJobStore]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    factory = make_session_factory(maker)
    try:
        yield PostgresJobStore(factory)
    finally:
        await engine.dispose()


async def _enqueue(
    store: PostgresJobStore, key: str = "j@k", name: str = "j", **kw: Any
) -> EnqueueResult:
    return await store.enqueue(
        job_name=name,
        idempotency_key=key,
        scheduled_for=kw.get("scheduled_for", at(0)),
        max_attempts=kw.get("max_attempts", 3),
        trigger_kind=TriggerKind.INTERVAL,
        available_at=kw.get("available_at"),
        payload=kw.get("payload"),
    )


async def test_enqueue_and_get(store: PostgresJobStore) -> None:
    res = await _enqueue(store, payload={"book_id": "b1"})
    assert res.created
    fetched = await store.get(res.run.id)
    assert fetched is not None
    assert fetched.status is JobRunStatus.PENDING
    assert fetched.payload == {"book_id": "b1"}


async def test_enqueue_dedups_via_partial_unique_index(store: PostgresJobStore) -> None:
    first = await _enqueue(store, key="dup")
    second = await _enqueue(store, key="dup")
    assert first.created
    assert not second.created
    assert second.run.id == first.run.id
    runs = await store.list_runs(job_name="j")
    assert len(runs) == 1


async def test_claim_exclusive_and_attempt_increments(store: PostgresJobStore) -> None:
    await _enqueue(store, key="solo")
    a = await store.claim_due(now=at(0), lease_seconds=60)
    b = await store.claim_due(now=at(0), lease_seconds=60)
    assert a is not None
    assert a.status is JobRunStatus.RUNNING
    assert a.attempt == 1
    assert a.lease_token is not None
    assert b is None


async def test_claim_respects_available_at(store: PostgresJobStore) -> None:
    await _enqueue(store, key="future", available_at=at(5))
    assert await store.claim_due(now=at(0), lease_seconds=60) is None
    assert await store.claim_due(now=at(5), lease_seconds=60) is not None


async def test_claim_filters_job_names(store: PostgresJobStore) -> None:
    await _enqueue(store, key="a", name="alpha")
    await _enqueue(store, key="b", name="beta")
    claimed = await store.claim_due(now=at(0), lease_seconds=60, job_names=["beta"])
    assert claimed is not None
    assert claimed.job_name == "beta"


async def test_complete_frees_key(store: PostgresJobStore) -> None:
    first = await _enqueue(store, key="cycle")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.complete(claimed.id, outcome=RunOutcome.SUCCESS, detail={"rows": 7})
    done = await store.get(first.run.id)
    assert done is not None
    assert done.status is JobRunStatus.SUCCEEDED
    assert done.detail == {"rows": 7}
    # Key freed -> a new active run can be created.
    second = await _enqueue(store, key="cycle")
    assert second.created
    assert second.run.id != first.run.id


async def test_retry_then_reclaim(store: PostgresJobStore) -> None:
    await _enqueue(store, key="retry")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.retry(claimed.id, available_at=at(0, 8), error="boom")
    assert await store.claim_due(now=at(0, 2), lease_seconds=60) is None
    again = await store.claim_due(now=at(0, 8), lease_seconds=60)
    assert again is not None
    assert again.attempt == 2


async def test_deadletter_and_listing(store: PostgresJobStore) -> None:
    await _enqueue(store, key="dead")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.deadletter(claimed.id, error="fatal")
    dl = await store.dead_letters()
    assert len(dl) == 1
    assert dl[0].status is JobRunStatus.DEADLETTER
    stats = await store.stats()
    assert stats.deadletter_total == 1


async def test_cancel(store: PostgresJobStore) -> None:
    res = await _enqueue(store, key="cancel")
    assert await store.cancel(res.run.id) is True
    run = await store.get(res.run.id)
    assert run is not None
    assert run.status is JobRunStatus.CANCELLED
    assert await store.cancel(res.run.id) is False


async def test_reap_expired_lease(store: PostgresJobStore) -> None:
    await _enqueue(store, key="lease")
    claimed = await store.claim_due(now=at(0), lease_seconds=30)
    assert claimed is not None
    assert await store.reap_expired(now=at(0, 20)) == 0
    assert await store.reap_expired(now=at(1)) == 1
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.RETRYING


async def test_stats_counts(store: PostgresJobStore) -> None:
    await _enqueue(store, key="s1")
    await _enqueue(store, key="s2")
    stats = await store.stats()
    assert stats.by_status.get("pending") == 2
    assert stats.active == 2


async def test_full_framework_over_postgres_store(store: PostgresJobStore) -> None:
    reg = JobRegistry()
    fired = {"n": 0}

    @job("pgjob", trigger=every(30), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        fired["n"] += 1
        return JobResult.ok()

    h = VirtualClockHarness(reg, store=store, start=at(0))
    await h.advance(30)
    await h.run_pending()
    await h.advance(30)
    await h.run_pending()
    assert fired["n"] == 2
    succeeded = await store.list_runs(status=JobRunStatus.SUCCEEDED)
    assert len(succeeded) == 2


async def test_retry_to_deadletter_over_postgres(store: PostgresJobStore) -> None:
    reg = JobRegistry()

    @job("pgfails", trigger=every(10), max_attempts=2, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        raise RuntimeError("postgres never works")

    h = VirtualClockHarness(reg, store=store, start=at(0), seed=0)
    await h.run_now("pgfails")
    for _ in range(6):
        await h.advance(300)
        await h.drain_worker()
    dl = await store.dead_letters()
    assert len(dl) >= 1
    assert "never works" in (dl[0].error or "")
