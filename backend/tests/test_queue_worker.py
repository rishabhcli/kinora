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

from app.agents.contracts import ConflictObject, ConflictOption, ConflictOptionSpec, ConflictType
from app.db.models.enums import RenderJobStatus, RenderPriority, ShotStatus
from app.queue.redis_queue import QueuedJob, RedisRenderQueue, session_channel
from app.queue.worker import RenderWorker
from app.redis.client import RedisClient
from app.render import degrade
from app.render.pipeline import RenderResult

_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")
_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
_S3_ENDPOINT = os.environ.get("KINORA_TEST_S3_ENDPOINT_URL") or os.environ.get(
    "KINORA_TEST_S3_ENDPOINT"
)

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
    messages = []
    async with redis_client.subscribe(channel) as pubsub:
        await asyncio.sleep(0.1)  # let the subscription register
        await worker.process_job(job)
        # clip_ready + the three crew events publish synchronously during
        # process_job, so drain briefly and stop as soon as they've all arrived
        # (no lingering timeout, which would perturb the timing-sensitive suite).
        for _ in range(10):
            m = await redis_client.next_message(pubsub, timeout=0.25)
            if m is None:
                break
            messages.append(m)
            crew = {x.get("agent") for x in messages if x["event"] == "agent_activity"}
            if any(x["event"] == "clip_ready" for x in messages) and {
                "cinematographer",
                "generator",
                "critic",
            } <= crew:
                break

    clip = next((m for m in messages if m["event"] == "clip_ready"), None)
    assert clip is not None
    assert clip["shot_id"] == "shot_x"
    assert clip["oss_url"].endswith("shot_x.mp4")
    # §5.4: the crew's planning + render + QA also surface in the live feed, so a
    # judge watches the Cinematographer/Generator/Critic work — not just the clip.
    agents = {m.get("agent") for m in messages if m["event"] == "agent_activity"}
    assert {"cinematographer", "generator", "critic"} <= agents
    done = await queue.get_job("job_ok")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED
    print(f"\n[WORKER] clip_ready + crew feed {sorted(agents)} on {channel}; "
          f"job -> {done.status.value}")


async def test_worker_conflict_publishes_conflict_events(
    queue: RedisRenderQueue, redis_client: RedisClient
) -> None:
    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    conflict = ConflictObject(
        conflict_id="cf_test",
        raised_by="Continuity",
        type=ConflictType.CANON_VIOLATION,
        shot_id="shot_cf",
        claim="She wears a red coat",
        canon_fact="She wears a blue coat",
        options=[
            ConflictOptionSpec(id=ConflictOption.HONOR_CANON, action="Honor canon (keep blue coat)")
        ],
    )

    async def fake_run_shot(job: QueuedJob) -> RenderResult:
        return RenderResult(
            shot_id=job.shot_id or "",
            status=ShotStatus.CONFLICT,
            rung="conflict",
            qa={"verdict": "conflict"},
            conflict=conflict,
            video_seconds=0.0,
        )

    worker = RenderWorker(queue, redis_client, run_shot=fake_run_shot, session_factory=None)
    await queue.enqueue(
        shot_hash="h_conflict",
        priority=RenderPriority.COMMITTED,
        book_id="book_demo",
        job_id="job_cf",
        session_id=session_id,
        shot_id="shot_cf",
        target_duration_s=5.0,
    )
    job = await queue.claim()
    assert job is not None

    channel = session_channel(session_id)
    messages: list[dict] = []
    async with redis_client.subscribe(channel) as pubsub:
        await asyncio.sleep(0.1)
        await worker.process_job(job)
        for _ in range(3):
            msg = await redis_client.next_message(pubsub, timeout=3.0)
            if isinstance(msg, dict):
                messages.append(msg)

    events = {m["event"] for m in messages}
    assert "conflict_choice" in events
    assert "agent_activity" in events
    assert not any(m.get("event") == "clip_ready" for m in messages)


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


# --- lease heartbeat: a slow render is not reaped mid-flight (Fix 11) -------- #


async def test_worker_heartbeats_lease_during_render(
    queue: RedisRenderQueue, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    original = queue.extend_lease

    async def counting_extend(job_id: str, **kwargs: object) -> bool:
        calls["n"] += 1
        return await original(job_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(queue, "extend_lease", counting_extend)

    async def slow_run_shot(job: QueuedJob) -> RenderResult:
        await asyncio.sleep(0.25)
        return RenderResult(
            shot_id=job.shot_id or "", status=ShotStatus.ACCEPTED, rung="full_video",
            video_seconds=5.0,
        )

    worker = RenderWorker(
        queue, redis_client, run_shot=slow_run_shot, session_factory=None,
        lease_heartbeat_s=0.05,
    )
    await queue.enqueue(
        shot_hash="slow_render", priority=RenderPriority.COMMITTED, book_id="b",
        job_id="job_slow", session_id="sess_x", shot_id="shot_s",
    )
    job = await queue.claim()
    assert job is not None

    await worker.process_job(job)

    assert calls["n"] >= 1  # the lease was heartbeated at least once during the render
    done = await queue.get_job("job_slow")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED
    print(f"\n[LEASE HEARTBEAT] worker extended the lease {calls['n']}x during a slow render")


# --- scene completion stitches + publishes scene_stitched (Fix 12, §9.6) ----- #


@pytest.mark.skipif(
    not (_DB_URL and _S3_ENDPOINT and degrade.ffmpeg_available()),
    reason="scene stitch needs KINORA_TEST_DATABASE_URL + S3 + ffmpeg",
)
async def test_scene_completion_stitches_and_publishes_absolute_sync_map(
    queue: RedisRenderQueue, redis_client: RedisClient
) -> None:
    import uuid as _uuid
    from contextlib import asynccontextmanager

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import models  # noqa: F401
    from app.db.base import Base
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.book import BookRepo
    from app.db.repositories.scene import SceneRepo
    from app.db.repositories.shot import ShotRepo
    from app.render.sync_map import SyncSegment, SyncWord
    from app.storage.object_store import ObjectStore, keys
    from tests.test_render_support import png_bytes, wav_bytes

    assert _DB_URL is not None and _S3_ENDPOINT is not None
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

    store = ObjectStore(
        endpoint_url=_S3_ENDPOINT,
        region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
        access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
        secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
        bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora"),
    )
    store.ensure_bucket()

    book_id = f"book_{_uuid.uuid4().hex[:8]}"
    scene_id = f"scene_{_uuid.uuid4().hex[:8]}"
    session_id = f"sess_{_uuid.uuid4().hex[:8]}"

    # Two real, playable clips uploaded to object storage.
    clip0 = degrade.ken_burns_over_image(png_bytes(320, 240), 1.0, audio_bytes=wav_bytes(1.0))
    clip1 = degrade.ken_burns_over_image(png_bytes(320, 240), 1.0, audio_bytes=wav_bytes(1.0))
    key0, key1 = keys.clip(book_id, "s0"), keys.clip(book_id, "s1")
    store.put_bytes(key0, clip0, "video/mp4")
    store.put_bytes(key1, clip1, "video/mp4")

    # Per-shot sync segments are LOCAL (video_start_s=0) — the shared contract.
    seg0 = SyncSegment(
        shot_id="s0", video_start_s=0.0, video_end_s=1.0, page=1, page_turn_at_s=0.8,
        words=[SyncWord(word_index=1, text="a", t_start=0.1, t_end=0.5, bbox=None)],
    )
    seg1 = SyncSegment(
        shot_id="s1", video_start_s=0.0, video_end_s=1.5, page=2, page_turn_at_s=1.3,
        words=[SyncWord(word_index=2, text="b", t_start=0.2, t_end=0.9, bbox=None)],
    )

    async with session_ctx() as db:
        await BookRepo(db).create(title="Stitch Book", book_id=book_id)
        await SceneRepo(db).create(
            book_id=book_id, scene_index=1, page_start=1, page_end=2, scene_id=scene_id
        )
        await BeatRepo(db).create(
            book_id=book_id, scene_id=scene_id, beat_index=1, summary="b0",
            entities=[], beat_id="b0",
        )
        await BeatRepo(db).create(
            book_id=book_id, scene_id=scene_id, beat_index=2, summary="b1",
            entities=[], beat_id="b1",
        )
        shots = ShotRepo(db)
        await shots.create(
            id="s0", book_id=book_id, scene_id=scene_id, beat_id="b0",
            status=ShotStatus.ACCEPTED, duration_s=1.0,
            output={"clip_key": key0}, narration={"sync_segment": seg0.model_dump(mode="json")},
        )
        await shots.create(
            id="s1", book_id=book_id, scene_id=scene_id, beat_id="b1",
            status=ShotStatus.ACCEPTED, duration_s=1.5,
            output={"clip_key": key1}, narration={"sync_segment": seg1.model_dump(mode="json")},
        )

    async def fake_run_shot(job: QueuedJob) -> RenderResult:
        return RenderResult(
            shot_id="s1", status=ShotStatus.ACCEPTED, rung="full_video",
            clip_key=key1, sync_segment=seg1.model_dump(mode="json"), video_seconds=1.5,
        )

    worker = RenderWorker(
        queue, redis_client, run_shot=fake_run_shot,
        session_factory=session_ctx, object_store=store,
    )
    await queue.enqueue(
        shot_hash="h_stitch", priority=RenderPriority.COMMITTED, book_id=book_id,
        job_id="job_s1", session_id=session_id, shot_id="s1", scene_id=scene_id,
    )
    job = await queue.claim()
    assert job is not None

    channel = session_channel(session_id)
    stitched: dict | None = None
    async with redis_client.subscribe(channel) as pubsub:
        await asyncio.sleep(0.1)  # let the subscription register
        await worker.process_job(job)
        for _ in range(6):
            msg = await redis_client.next_message(pubsub, timeout=8.0)
            if isinstance(msg, dict) and msg.get("event") == "scene_stitched":
                stitched = msg
                break

    await engine.dispose()

    assert stitched is not None
    assert stitched["scene_id"] == scene_id
    assert stitched["oss_url"]
    segs = stitched["sync_map"]["segments"]
    assert len(segs) == 2
    # ABSOLUTE video time: shot 1 starts at shot 0's length, words shifted likewise.
    assert segs[0]["video_start_s"] == 0.0
    assert segs[1]["video_start_s"] == pytest.approx(1.0)
    assert segs[1]["words"][0]["t_start"] == pytest.approx(1.2)
    print(f"\n[SCENE STITCH] scene {scene_id} stitched on completion; "
          f"sync map in ABSOLUTE time: seg[1].video_start_s={segs[1]['video_start_s']}")


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
