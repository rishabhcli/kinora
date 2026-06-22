"""Redis priority-queue tests against a throwaway Redis (kinora.md §12.1–§12.3).

Exercises the *real* :class:`RedisRenderQueue` (Lua + sorted sets) — never a fake
— covering idempotency, cross-session dedup, lane priority, committed→speculative
preemption, depth backpressure, cooperative cancellation, and the
exponential-backoff→DLQ retry path. The optional Postgres mirror (durable
``render_jobs`` rows) is verified when a test database is configured.

SKIPs cleanly unless ``KINORA_TEST_REDIS_URL`` is set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.redis_queue import EnqueueStatus, RedisRenderQueue, RetryDecision
from app.redis.client import RedisClient

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")
_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping queue tests"
)

_BASE_MS = 1_700_000_000_000


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[RedisClient]:
    """A real Redis client for the throwaway server."""
    assert _REDIS_URL is not None
    client = RedisClient.from_url(_REDIS_URL)
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def queue(redis_client: RedisClient) -> AsyncIterator[RedisRenderQueue]:
    """A queue in an isolated namespace, purged on teardown."""
    ns = f"kinora:test:rq:{uuid.uuid4().hex[:10]}"
    q = RedisRenderQueue(
        redis_client,
        namespace=ns,
        backpressure_depth=3,
        retry_cap=2,
        retry_backoff_s=(2.0, 8.0, 30.0),
    )
    try:
        yield q
    finally:
        await q.purge()


async def _enqueue(
    q: RedisRenderQueue,
    shot_hash: str,
    priority: RenderPriority = RenderPriority.COMMITTED,
    **kw: object,
) -> object:
    return await q.enqueue(
        shot_hash=shot_hash,
        priority=priority,
        book_id=kw.pop("book_id", "book_demo"),  # type: ignore[arg-type]
        job_id=kw.pop("job_id", uuid.uuid4().hex),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# --- idempotency / dedup (§12.3) -------------------------------------------- #


async def test_idempotency_same_shot_hash_returns_same_job(queue: RedisRenderQueue) -> None:
    first = await queue.enqueue(
        shot_hash="hash_A", priority=RenderPriority.COMMITTED, book_id="b", job_id="job_1"
    )
    second = await queue.enqueue(
        shot_hash="hash_A", priority=RenderPriority.COMMITTED, book_id="b", job_id="job_2"
    )

    assert first.status is EnqueueStatus.ENQUEUED and first.job_id == "job_1"
    assert second.status is EnqueueStatus.EXISTING
    assert second.job_id == "job_1"  # the duplicate collapses to the first job
    assert await queue.depth(RenderPriority.COMMITTED) == 1  # never a second job
    print(f"\n[IDEMPOTENCY] enqueue#1 -> {first.job_id} (created); "
          f"enqueue#2 (same shot_hash) -> {second.job_id} ({second.status.value}); "
          f"committed depth == {await queue.depth(RenderPriority.COMMITTED)}")


async def test_cross_session_dedup(queue: RedisRenderQueue) -> None:
    # Two different sessions request the identical shot simultaneously.
    a = await queue.enqueue(
        shot_hash="shared", priority=RenderPriority.COMMITTED, book_id="b",
        job_id="jA", session_id="sess_A",
    )
    b = await queue.enqueue(
        shot_hash="shared", priority=RenderPriority.COMMITTED, book_id="b",
        job_id="jB", session_id="sess_B",
    )
    assert a.created and a.job_id == "jA"
    assert b.status is EnqueueStatus.EXISTING and b.job_id == "jA"
    assert await queue.depth() == 1  # paid for once, not twice


# --- priority (§4.9/§12.2) -------------------------------------------------- #


async def test_priority_committed_pulled_before_speculative(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "spec1", RenderPriority.SPECULATIVE, job_id="js")
    await _enqueue(queue, "comm1", RenderPriority.COMMITTED, job_id="jc")
    await _enqueue(queue, "kf1", RenderPriority.KEYFRAME, job_id="jk")

    first = await queue.claim()
    second = await queue.claim()
    third = await queue.claim()
    assert first is not None and first.id == "jc"  # committed first
    assert second is not None and second.id == "js"  # then speculative
    assert third is not None and third.id == "jk"  # then keyframe


# --- backpressure (§12.2) --------------------------------------------------- #


async def test_backpressure_drops_speculative_admits_committed(queue: RedisRenderQueue) -> None:
    # backpressure_depth == 3 (fixture).
    for i in range(3):
        res = await _enqueue(queue, f"s{i}", RenderPriority.SPECULATIVE, job_id=f"s{i}")
        assert res.created  # type: ignore[attr-defined]

    dropped = await _enqueue(queue, "s_over", RenderPriority.SPECULATIVE, job_id="s_over")
    admitted = await _enqueue(queue, "c_always", RenderPriority.COMMITTED, job_id="c_always")

    assert dropped.status is EnqueueStatus.DROPPED  # type: ignore[attr-defined]
    assert dropped.job_id is None  # type: ignore[attr-defined]
    assert admitted.created  # committed is always admitted  # type: ignore[attr-defined]
    stats = await queue.stats()
    assert stats.dropped_total == 1
    print(f"\n[BACKPRESSURE] depth cap=3: 4th speculative -> dropped; "
          f"committed -> admitted; dropped_total={stats.dropped_total}")


# --- preemption (§4.9) ------------------------------------------------------ #


async def test_committed_enqueue_preempts_inflight_speculative(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "spec", RenderPriority.SPECULATIVE, job_id="spec")
    claimed = await queue.claim(lanes=[RenderPriority.SPECULATIVE])
    assert claimed is not None and claimed.id == "spec"
    assert not await queue.is_cancelled("spec")

    # A committed enqueue must mark the in-flight speculative job cancellable.
    await _enqueue(queue, "comm", RenderPriority.COMMITTED, job_id="comm")
    assert await queue.is_cancelled("spec") is True
    print("\n[PREEMPTION] committed enqueue flagged in-flight speculative 'spec' cancellable")


# --- cancellation (§4.7/§4.8/§12.1) ----------------------------------------- #


async def test_cancel_by_token_is_lane_scoped(queue: RedisRenderQueue) -> None:
    token = "traj_x"
    await _enqueue(queue, "c", RenderPriority.COMMITTED, job_id="c", cancel_token=token)
    await _enqueue(queue, "s", RenderPriority.SPECULATIVE, job_id="s", cancel_token=token)
    await _enqueue(queue, "k", RenderPriority.KEYFRAME, job_id="k", cancel_token=token)

    # Idle-pause cancels speculative + keyframe but preserves the committed buffer.
    count = await queue.cancel_by_token(
        token, lanes=[RenderPriority.SPECULATIVE, RenderPriority.KEYFRAME]
    )
    assert count == 2
    assert await queue.is_cancelled("s") is True
    assert await queue.is_cancelled("k") is True
    assert await queue.is_cancelled("c") is False  # committed frozen, not cancelled


async def test_cancel_distant_cancels_far_speculative_only(queue: RedisRenderQueue) -> None:
    token = "traj_seek"
    await _enqueue(
        queue, "near", RenderPriority.SPECULATIVE, job_id="near",
        cancel_token=token, target_word=200,
    )
    await _enqueue(
        queue, "far", RenderPriority.SPECULATIVE, job_id="far",
        cancel_token=token, target_word=100_000,
    )
    # At w=0, v=4: eta(near)=50s (<120 keep), eta(far)=25000s (>120 cancel).
    cancelled = await queue.cancel_distant(
        token, focus_word=0, velocity_wps=4.0, threshold_s=120.0
    )
    assert cancelled == 1
    assert await queue.is_cancelled("far") is True
    assert await queue.is_cancelled("near") is False


# --- retries → DLQ (§12.1) -------------------------------------------------- #


async def test_retry_backoff_then_deadletter(queue: RedisRenderQueue) -> None:
    await queue.enqueue(
        shot_hash="flaky", priority=RenderPriority.COMMITTED, book_id="b",
        job_id="flaky", now_ms=_BASE_MS,
    )
    claimed = await queue.claim(now_ms=_BASE_MS)
    assert claimed is not None

    # Attempt 1 fails -> backoff 2s.
    out1 = await queue.retry("flaky", error="503", now_ms=_BASE_MS)
    assert out1.decision is RetryDecision.RETRY and out1.attempts == 1 and out1.delay_s == 2.0
    # Not yet ready 1.9s later; ready at +2s.
    assert await queue.claim(now_ms=_BASE_MS + 1_900) is None
    again = await queue.claim(now_ms=_BASE_MS + 2_000)
    assert again is not None

    # Attempt 2 fails -> backoff 8s.
    out2 = await queue.retry("flaky", error="timeout", now_ms=_BASE_MS + 2_000)
    assert out2.decision is RetryDecision.RETRY and out2.attempts == 2 and out2.delay_s == 8.0
    claimed3 = await queue.claim(now_ms=_BASE_MS + 10_000)
    assert claimed3 is not None

    # Attempt 3 exhausts the cap (2) -> dead-letter.
    out3 = await queue.retry("flaky", error="boom", now_ms=_BASE_MS + 10_000)
    assert out3.decision is RetryDecision.DEADLETTER and out3.attempts == 3

    job = await queue.get_job("flaky")
    assert job is not None and job.status is RenderJobStatus.DEADLETTER
    assert await queue.dlq_len() == 1
    # Dead-letter clears the idempotency index (a fresh attempt may be enqueued).
    assert await queue.lookup("flaky") is None
    stats = await queue.stats()
    print(f"\n[DLQ] flaky job: retry(2s) -> retry(8s) -> DEADLETTER after cap=2; "
          f"dlq_len={await queue.dlq_len()} deadletter_total={stats.deadletter_total}")


async def test_depth_and_stats_snapshot(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c1", RenderPriority.COMMITTED, job_id="c1")
    await _enqueue(queue, "c2", RenderPriority.COMMITTED, job_id="c2")
    await _enqueue(queue, "s1", RenderPriority.SPECULATIVE, job_id="s1")
    claimed = await queue.claim(lanes=[RenderPriority.COMMITTED])
    assert claimed is not None
    await queue.ack(claimed.id)

    stats = await queue.stats()
    assert stats.depths[RenderPriority.COMMITTED.value] == 1  # c2 still queued
    assert stats.depths[RenderPriority.SPECULATIVE.value] == 1
    assert stats.enqueued_total == 3
    assert stats.succeeded_total == 1
    assert await queue.depth() == 2


# --- durable Postgres mirror (render_jobs) ---------------------------------- #


@pytest.mark.skipif(not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set")
async def test_render_jobs_mirror_lifecycle(redis_client: RedisClient) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import models  # noqa: F401  (register tables)
    from app.db.base import Base
    from app.db.repositories.render_job import RenderJobRepo

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_ctx() -> AsyncIterator[object]:
        db = factory()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    ns = f"kinora:test:rq:{uuid.uuid4().hex[:10]}"
    q = RedisRenderQueue(redis_client, namespace=ns, session_factory=session_ctx)
    try:
        job_id = uuid.uuid4().hex
        await q.enqueue(
            shot_hash=f"h_{job_id}", priority=RenderPriority.COMMITTED,
            book_id="b", job_id=job_id, reserved_video_s=5.0,
        )
        async with session_ctx() as db:
            row = await RenderJobRepo(db).get(job_id)  # type: ignore[arg-type]
        assert row is not None and row.status is RenderJobStatus.QUEUED
        assert row.priority is RenderPriority.COMMITTED

        claimed = await q.claim()
        assert claimed is not None
        await q.ack(claimed.id)
        async with session_ctx() as db:
            row2 = await RenderJobRepo(db).get(job_id)  # type: ignore[arg-type]
        assert row2 is not None and row2.status is RenderJobStatus.SUCCEEDED
        print(f"\n[DB MIRROR] render_jobs[{job_id[:8]}]: queued -> succeeded (durable)")
    finally:
        await q.purge()
        await engine.dispose()
