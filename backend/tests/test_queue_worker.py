"""Render-worker tests (kinora.md §12.1).

Drives the *real* :class:`RenderWorker` against a throwaway Redis with an injected
:class:`RenderPipeline` double (the heavy DashScope/ffmpeg work is out of scope
here):

* a successful render publishes ``clip_ready`` on the session channel and acks;
* a cancelled job releases its reserved budget earmark (proved against the real
  Postgres budget ledger) and finalizes as cancelled — **zero** video-seconds;
* a job that keeps failing backs off and dead-letters after the retry cap.

SKIPs cleanly unless ``KINORA_TEST_REDIS_URL`` is set (the budget test also needs
``KINORA_TEST_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from app.db.models.enums import RenderJobStatus, RenderPriority, ShotStatus
from app.queue.redis_queue import QueuedJob, RedisRenderQueue, session_channel
from app.queue.worker import RenderWorker
from app.redis.client import RedisClient
from app.render.pipeline import RenderResult

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")
_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="KINORA_TEST_REDIS_URL not set; skipping worker tests"
)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[RedisClient]:
    assert _REDIS_URL is not None
    client = RedisClient.from_url(_REDIS_URL)
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def queue(redis_client: RedisClient) -> AsyncIterator[RedisRenderQueue]:
    ns = f"kinora:test:rq:{uuid.uuid4().hex[:10]}"
    q = RedisRenderQueue(
        redis_client, namespace=ns, retry_cap=2, retry_backoff_s=(0.01, 0.02, 0.03)
    )
    try:
        yield q
    finally:
        await q.purge()


# --- success: publishes clip_ready ------------------------------------------ #


async def test_worker_success_publishes_clip_ready(
    queue: RedisRenderQueue, redis_client: RedisClient
) -> None:
    session_id = f"sess_{uuid.uuid4().hex[:8]}"

    async def fake_run_shot(job: QueuedJob) -> RenderResult:
        return RenderResult(
            shot_id=job.shot_id or "",
            status=ShotStatus.ACCEPTED,
            rung="full_video",
            clip_key="clips/book_demo/shot_x.mp4",
            clip_url="https://oss.test/clips/book_demo/shot_x.mp4",
            sync_segment={"shot_id": "shot_x", "page_turn_at_s": None},
            qa={"verdict": "pass", "ccs": 0.93},
            video_seconds=5.0,
        )

    worker = RenderWorker(queue, redis_client, run_shot=fake_run_shot, session_factory=None)

    await queue.enqueue(
        shot_hash="h_success", priority=RenderPriority.COMMITTED, book_id="book_demo",
        job_id="job_ok", session_id=session_id, shot_id="shot_x", target_duration_s=5.0,
    )
    job = await queue.claim()
    assert job is not None

    channel = session_channel(session_id)
    async with redis_client.subscribe(channel) as pubsub:
        await asyncio.sleep(0.1)  # let the subscription register
        await worker.process_job(job)
        message = await redis_client.next_message(pubsub, timeout=3.0)

    assert message is not None
    assert message["event"] == "clip_ready"
    assert message["shot_id"] == "shot_x"
    assert message["oss_url"].endswith("shot_x.mp4")
    done = await queue.get_job("job_ok")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED
    print(f"\n[WORKER] clip_ready published on {channel}: shot_id={message['shot_id']} "
          f"rung={message['rung']}; job -> {done.status.value}")


# --- failure: dead-letters after the retry cap ------------------------------ #


async def test_worker_failing_job_deadletters(
    queue: RedisRenderQueue, redis_client: RedisClient
) -> None:
    attempts = {"n": 0}

    async def always_fails(job: QueuedJob) -> RenderResult:
        attempts["n"] += 1
        raise RuntimeError("transient provider boom")

    worker = RenderWorker(queue, redis_client, run_shot=always_fails, session_factory=None)
    await queue.enqueue(
        shot_hash="h_flaky", priority=RenderPriority.COMMITTED, book_id="b",
        job_id="job_dlq", shot_id="shot_d",
    )

    # Three failures (cap=2 => 3rd dead-letters). Sleep past the tiny backoff.
    for _ in range(3):
        processed = await worker.process_once(lanes=[RenderPriority.COMMITTED])
        assert processed is True
        await asyncio.sleep(0.05)

    job = await queue.get_job("job_dlq")
    assert job is not None and job.status is RenderJobStatus.DEADLETTER
    assert job.attempts == 3
    assert await queue.dlq_len() == 1
    print(f"\n[WORKER DLQ] shot rendered {attempts['n']}x, all failed -> "
          f"job {job.status.value} (attempts={job.attempts}), dlq_len={await queue.dlq_len()}")


# --- cancellation: releases the reserved budget (real ledger) --------------- #


@pytest.mark.skipif(not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set")
async def test_worker_cancelled_job_releases_budget(
    queue: RedisRenderQueue, redis_client: RedisClient
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import models  # noqa: F401
    from app.db.base import Base
    from app.db.repositories.book import BookRepo
    from app.db.repositories.budget import BudgetRepo
    from app.db.repositories.session import SessionRepo
    from app.memory.budget_service import BudgetLimits, BudgetService

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_ctx() -> AsyncIterator[AsyncSession]:
        db = factory()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    limits = BudgetLimits(
        ceiling_video_s=1650, per_session_s=300, per_scene_s=90, low_floor_s=120, live_video=True
    )

    # Real book + session rows (the budget ledger references them by FK).
    async with session_ctx() as db:
        book = await BookRepo(db).create(title="Budget Book")
        book_id = book.id
        await SessionRepo(db).upsert(session_id=session_id, book_id=book_id)

    # The Scheduler's gating earmark for a promotion.
    async with session_ctx() as db:
        reservation = await BudgetService(repo=BudgetRepo(db), limits=limits).reserve(
            5.0, session_id=session_id, scene_id="scene_c", book_id=book_id
        )
    async with session_ctx() as db:
        used_before = await BudgetRepo(db).used_seconds(session_id=session_id)
    assert used_before == 5.0  # earmark is outstanding

    async def never_called(job: QueuedJob) -> RenderResult:  # pragma: no cover
        raise AssertionError("cancelled job must not render")

    worker = RenderWorker(
        queue,
        redis_client,
        run_shot=never_called,
        session_factory=session_ctx,
        budget_factory=lambda db: BudgetService(repo=BudgetRepo(db), limits=limits),
    )

    await queue.enqueue(
        shot_hash="h_cancel", priority=RenderPriority.COMMITTED, book_id=book_id,
        job_id="job_cancel", session_id=session_id, shot_id="shot_c", scene_id="scene_c",
        reservation_id=reservation.id, reserved_video_s=5.0, target_duration_s=5.0,
    )
    await queue.mark_cancelled("job_cancel")
    job = await queue.claim()
    assert job is not None and job.cancelled is True

    await worker.process_job(job)

    async with session_ctx() as db:
        used_after = await BudgetRepo(db).used_seconds(session_id=session_id)
    done = await queue.get_job("job_cancel")
    assert done is not None and done.status is RenderJobStatus.CANCELLED
    assert used_after == 0.0  # the earmark was released -> zero video-seconds spent
    print(f"\n[WORKER CANCEL] earmark {used_before}s -> released -> {used_after}s; "
          f"job -> {done.status.value} (no render, no spend)")
    await engine.dispose()
