"""Redis-backed durable store tests (require an isolated test Redis, e.g. db 15).

These re-run the in-memory store's contract against the real Redis implementation
to prove the Lua enqueue/claim paths preserve the same at-least-once + idempotent
semantics. They also drive the full framework end-to-end through the harness with
the Redis store substituted in.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from app.jobs.harness import VirtualClockHarness
from app.jobs.redis_store import RedisJobStore
from app.jobs.registry import JobRegistry, job
from app.jobs.store import EnqueueResult
from app.jobs.triggers import every
from app.jobs.types import JobContext, JobResult, JobRunStatus, RunOutcome, TriggerKind

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping redis store tests"
)


def at(mi: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, mi, s, tzinfo=UTC)


@pytest_asyncio.fixture
async def store() -> AsyncIterator[RedisJobStore]:
    assert _REDIS_URL is not None
    from redis.asyncio import Redis

    # single_connection_client avoids the shared (URL-keyed) connection pool being
    # reused across the per-test event loops the autouse fixtures churn — a reuse
    # that can return a stale read and flake an otherwise-deterministic claim.
    client = Redis.from_url(_REDIS_URL, decode_responses=True, single_connection_client=True)
    # Unique namespace per test so the autouse db-15 flush (conftest._isolate_state)
    # and a sibling test's teardown purge can never strand or collide on keys.
    s = RedisJobStore(client, namespace=f"kinora:jobstest:{uuid.uuid4().hex}")
    try:
        await s.purge()
        yield s
    finally:
        await s.purge()
        await client.aclose()  # type: ignore[attr-defined]


async def _enqueue(
    store: RedisJobStore, key: str = "j@k", name: str = "j", **kw: Any
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


async def test_enqueue_and_get(store: RedisJobStore) -> None:
    res = await _enqueue(store, payload={"book_id": "b1"})
    assert res.created
    fetched = await store.get(res.run.id)
    assert fetched is not None
    assert fetched.status is JobRunStatus.PENDING
    assert fetched.payload == {"book_id": "b1"}
    assert fetched.trigger_kind is TriggerKind.INTERVAL


async def test_enqueue_dedups_on_active_key(store: RedisJobStore) -> None:
    first = await _enqueue(store, key="dup")
    second = await _enqueue(store, key="dup")
    assert first.created
    assert not second.created
    assert second.run.id == first.run.id
    stats = await store.stats()
    assert stats.enqueued_total == 1


async def test_claim_is_exclusive_and_increments_attempt(store: RedisJobStore) -> None:
    await _enqueue(store, key="solo")
    a = await store.claim_due(now=at(0), lease_seconds=60)
    b = await store.claim_due(now=at(0), lease_seconds=60)
    assert a is not None
    assert a.status is JobRunStatus.RUNNING
    assert a.attempt == 1
    assert a.lease_token is not None
    assert b is None


async def test_claim_respects_available_at(store: RedisJobStore) -> None:
    await _enqueue(store, key="future", available_at=at(5))
    assert await store.claim_due(now=at(0), lease_seconds=60) is None
    assert await store.claim_due(now=at(5), lease_seconds=60) is not None


async def test_claim_filters_by_job_name(store: RedisJobStore) -> None:
    await _enqueue(store, key="a", name="alpha")
    await _enqueue(store, key="b", name="beta")
    claimed = await store.claim_due(now=at(0), lease_seconds=60, job_names=["beta"])
    assert claimed is not None
    assert claimed.job_name == "beta"
    # The skipped alpha is re-parked and still claimable.
    other = await store.claim_due(now=at(0), lease_seconds=60, job_names=["alpha"])
    assert other is not None
    assert other.job_name == "alpha"


async def test_complete_clears_key_and_allows_new_run(store: RedisJobStore) -> None:
    first = await _enqueue(store, key="cycle")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.complete(claimed.id, outcome=RunOutcome.SUCCESS, detail={"rows": 5})
    done = await store.get(first.run.id)
    assert done is not None
    assert done.status is JobRunStatus.SUCCEEDED
    assert done.detail == {"rows": 5}
    second = await _enqueue(store, key="cycle")
    assert second.created
    assert second.run.id != first.run.id


async def test_retry_then_reclaim(store: RedisJobStore) -> None:
    await _enqueue(store, key="retry")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.retry(claimed.id, available_at=at(0, 8), error="boom")
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.RETRYING
    assert run.error == "boom"
    assert await store.claim_due(now=at(0, 2), lease_seconds=60) is None
    again = await store.claim_due(now=at(0, 8), lease_seconds=60)
    assert again is not None
    assert again.attempt == 2


async def test_deadletter_lists_in_dlq(store: RedisJobStore) -> None:
    await _enqueue(store, key="dead")
    claimed = await store.claim_due(now=at(0), lease_seconds=60)
    assert claimed is not None
    await store.deadletter(claimed.id, error="fatal")
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.DEADLETTER
    dl = await store.dead_letters()
    assert len(dl) == 1
    assert dl[0].id == claimed.id
    stats = await store.stats()
    assert stats.deadletter_total == 1


async def test_cancel_pending(store: RedisJobStore) -> None:
    res = await _enqueue(store, key="cancel")
    assert await store.cancel(res.run.id) is True
    run = await store.get(res.run.id)
    assert run is not None
    assert run.status is JobRunStatus.CANCELLED
    assert await store.cancel(res.run.id) is False


async def test_reap_expired_lease(store: RedisJobStore) -> None:
    await _enqueue(store, key="lease")
    claimed = await store.claim_due(now=at(0), lease_seconds=30)
    assert claimed is not None
    assert await store.reap_expired(now=at(0, 20)) == 0
    assert await store.reap_expired(now=at(1)) == 1
    run = await store.get(claimed.id)
    assert run is not None
    assert run.status is JobRunStatus.RETRYING


async def test_list_runs_filters(store: RedisJobStore) -> None:
    await _enqueue(store, key="x1", name="x")
    await _enqueue(store, key="y1", name="y")
    xs = await store.list_runs(job_name="x")
    assert len(xs) == 1
    pendings = await store.list_runs(status=JobRunStatus.PENDING)
    assert len(pendings) == 2


async def test_full_framework_over_redis_store(store: RedisJobStore) -> None:
    reg = JobRegistry()
    fired = {"n": 0}

    @job("redisjob", trigger=every(30), registry=reg)
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


async def test_retry_to_deadletter_over_redis(store: RedisJobStore) -> None:
    reg = JobRegistry()

    @job("alwaysfails", trigger=every(10), max_attempts=2, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        raise RuntimeError("never works")

    h = VirtualClockHarness(reg, store=store, start=at(0), seed=0)
    await h.run_now("alwaysfails")  # enqueue one run directly
    for _ in range(6):
        await h.advance(300)
        await h.drain_worker()
    dl = await store.dead_letters()
    assert len(dl) >= 1
    assert "never works" in (dl[0].error or "")


async def test_namespace_isolation(store: RedisJobStore) -> None:
    # A different namespace shares the client but not the keyspace.
    from redis.asyncio import Redis

    assert _REDIS_URL is not None
    client = Redis.from_url(_REDIS_URL, decode_responses=True, single_connection_client=True)
    other = RedisJobStore(client, namespace=f"kinora:jobstest:other:{uuid.uuid4().hex}")
    await other.purge()
    try:
        await _enqueue(store, key="shared")
        # ``other`` sees none of ``store``'s runs.
        assert await other.list_runs() == []
    finally:
        await other.purge()
        await client.aclose()  # type: ignore[attr-defined]


async def test_available_at_in_future_with_timedelta(store: RedisJobStore) -> None:
    base = at(0)
    await _enqueue(store, key="td", available_at=base + timedelta(seconds=120))
    assert await store.claim_due(now=base, lease_seconds=60) is None
    assert await store.claim_due(now=base + timedelta(seconds=120), lease_seconds=60) is not None
