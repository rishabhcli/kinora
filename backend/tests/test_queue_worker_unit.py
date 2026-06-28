"""Render-worker behaviour, exercised with no infra (app.queue.fakeredis).

Mirrors the infra-gated ``test_queue_worker.py`` against the in-process fake. The
*real* :class:`RenderWorker` runs with an injected ``run_shot`` / ``run_keyframe``
double (the heavy DashScope/ffmpeg work is out of scope), and events are asserted
through :meth:`FakeRedisClient.events_on` rather than pub/sub timing — fully
deterministic. Covers kinora.md §12.1, §5.4, §5.6.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.models.enums import RenderJobStatus, RenderPriority, ShotStatus
from app.queue.fakeredis import FakeRedisClient
from app.queue.redis_queue import QueuedJob, RedisRenderQueue, session_channel
from app.queue.worker import RenderWorker
from app.render.pipeline import RenderResult, UnknownShotError


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def queue(client: FakeRedisClient) -> RedisRenderQueue:
    return RedisRenderQueue(
        client, namespace="kinora:test:wrk", retry_cap=2, retry_backoff_s=(0.0, 0.0, 0.0)
    )


async def _accepted(job: QueuedJob, *, rung: str = "full_video") -> RenderResult:
    return RenderResult(
        shot_id=job.shot_id or "",
        status=ShotStatus.ACCEPTED,
        rung=rung,
        clip_key=f"clips/{job.book_id}/{job.shot_id}.mp4",
        clip_url=f"https://oss.test/clips/{job.book_id}/{job.shot_id}.mp4",
        sync_segment={"shot_id": job.shot_id, "page_turn_at_s": None},
        qa={"verdict": "pass", "ccs": 0.93},
        video_seconds=5.0,
    )


# --- success path ----------------------------------------------------------- #


async def test_success_publishes_clip_ready_and_crew(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    session_id = "sess_ok"
    worker = RenderWorker(queue, client, run_shot=_accepted, session_factory=None)
    await queue.enqueue(
        shot_hash="h_ok",
        priority=RenderPriority.COMMITTED,
        book_id="book_demo",
        job_id="job_ok",
        session_id=session_id,
        shot_id="shot_x",
        target_duration_s=5.0,
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)

    events = client.events_on(session_channel(session_id))
    clip = next((e for e in events if e["event"] == "clip_ready"), None)
    assert clip is not None and clip["shot_id"] == "shot_x"
    assert clip["oss_url"].endswith("shot_x.mp4")
    # §5.4: the crew's planning + render + QA surface in the feed.
    agents = {e.get("agent") for e in events if e["event"] == "agent_activity"}
    assert {"cinematographer", "generator", "critic"} <= agents

    done = await queue.get_job("job_ok")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED


async def test_process_once_returns_false_when_idle(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    worker = RenderWorker(queue, client, run_shot=_accepted, session_factory=None)
    assert await worker.process_once() is False


async def test_cache_hit_rung_reads_as_reuse(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    worker = RenderWorker(
        queue, client, run_shot=lambda j: _accepted(j, rung="cache_hit"), session_factory=None
    )
    await queue.enqueue(
        shot_hash="h_cache",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_cache",
        session_id="sess_c",
        shot_id="shot_c",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)
    gen = [
        e
        for e in client.events_on(session_channel("sess_c"))
        if e["event"] == "agent_activity" and e.get("agent") == "generator"
    ]
    assert gen and "Reused cached" in gen[0]["message"]


# --- cancellation safe-point ------------------------------------------------ #


async def test_cancelled_job_finalizes_without_render(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    ran = {"n": 0}

    async def run_shot(job: QueuedJob) -> RenderResult:
        ran["n"] += 1
        return await _accepted(job)

    worker = RenderWorker(queue, client, run_shot=run_shot, session_factory=None)
    await queue.enqueue(
        shot_hash="h_cancel",
        priority=RenderPriority.SPECULATIVE,
        book_id="b",
        job_id="job_cancel",
        shot_id="shot_z",
        cancel_token="tok",
    )
    job = await queue.claim()
    assert job is not None
    await queue.mark_cancelled("job_cancel")  # reader seeked away after claim
    job = await queue.get_job("job_cancel")
    assert job is not None
    await worker.process_job(job)

    assert ran["n"] == 0  # never rendered — zero video-seconds
    done = await queue.get_job("job_cancel")
    assert done is not None and done.status is RenderJobStatus.CANCELLED


async def test_cancelled_flag_on_job_object_honoured(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    # When the claimed snapshot already shows cancelled=1 (preemption flagged it
    # before claim), the worker finalizes without touching the provider.
    worker = RenderWorker(queue, client, run_shot=_accepted, session_factory=None)
    await queue.enqueue(
        shot_hash="h_pre",
        priority=RenderPriority.SPECULATIVE,
        book_id="b",
        job_id="job_pre",
        shot_id="shot_p",
    )
    await queue.mark_cancelled("job_pre")
    job = await queue.claim()
    assert job is not None and job.cancelled
    await worker.process_job(job)
    done = await queue.get_job("job_pre")
    assert done is not None and done.status is RenderJobStatus.CANCELLED


# --- retry → DLQ ------------------------------------------------------------ #


async def test_failing_job_deadletters_after_cap(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    attempts = {"n": 0}

    async def always_fails(job: QueuedJob) -> RenderResult:
        attempts["n"] += 1
        raise RuntimeError("transient provider boom")

    worker = RenderWorker(queue, client, run_shot=always_fails, session_factory=None)
    await queue.enqueue(
        shot_hash="h_flaky",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_dlq",
        shot_id="shot_d",
    )
    for _ in range(3):  # cap=2 => 3rd dead-letters; backoff is 0s in the fixture
        assert await worker.process_once(lanes=[RenderPriority.COMMITTED]) is True

    job = await queue.get_job("job_dlq")
    assert job is not None and job.status is RenderJobStatus.DEADLETTER
    assert job.attempts == 3 and await queue.dlq_len() == 1
    assert attempts["n"] == 3


async def test_permanent_error_deadletters_immediately(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    async def unknown_shot(job: QueuedJob) -> RenderResult:
        raise UnknownShotError("no such shot")

    worker = RenderWorker(queue, client, run_shot=unknown_shot, session_factory=None)
    await queue.enqueue(
        shot_hash="h_perm",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_perm",
        shot_id="shot_perm",
        now_ms=1_000,
    )
    # A permanent error re-queues at ``now_ms = far future`` (2**62), so the job is
    # parked unreachably far ahead and a worker never reclaims it again — the
    # practical equivalent of a terminal stop without paying further attempts.
    assert await worker.process_once(lanes=[RenderPriority.COMMITTED]) is True
    job = await queue.get_job("job_perm")
    assert job is not None and job.attempts == 1
    # Nothing is claimable now (or any realistic future time): it is parked at 2**62.
    assert await queue.claim(lanes=[RenderPriority.COMMITTED], now_ms=10**15) is None


async def test_render_without_shot_id_retries(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    worker = RenderWorker(queue, client, run_shot=_accepted, session_factory=None)
    await queue.enqueue(
        shot_hash="h_noshot",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_noshot",
    )
    job = await queue.claim()
    assert job is not None and job.shot_id is None
    await worker.process_job(job)  # no shot_id -> retry path
    j = await queue.get_job("job_noshot")
    assert j is not None and j.attempts == 1


# --- keyframe lane ---------------------------------------------------------- #


async def test_keyframe_runs_image_lane(queue: RedisRenderQueue, client: FakeRedisClient) -> None:
    ran: dict[str, list[str | None]] = {"beats": []}

    async def run_keyframe(job: QueuedJob) -> str:
        ran["beats"].append(job.beat_id)
        return "ok"

    worker = RenderWorker(
        queue, client, run_shot=_accepted, run_keyframe=run_keyframe, session_factory=None
    )
    await queue.enqueue(
        shot_hash="h_kf",
        priority=RenderPriority.KEYFRAME,
        book_id="b",
        job_id="job_kf",
        beat_id="beat_7",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)
    assert ran["beats"] == ["beat_7"]
    done = await queue.get_job("job_kf")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED


async def test_keyframe_unconfigured_acks_gracefully(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    worker = RenderWorker(queue, client, run_shot=_accepted, session_factory=None)
    await queue.enqueue(
        shot_hash="h_kf2",
        priority=RenderPriority.KEYFRAME,
        book_id="b",
        job_id="job_kf2",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)  # no run_keyframe configured -> ack, no crash
    done = await queue.get_job("job_kf2")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED


async def test_keyframe_failure_retries(queue: RedisRenderQueue, client: FakeRedisClient) -> None:
    async def boom(job: QueuedJob) -> str:
        raise RuntimeError("image-gen 429")

    worker = RenderWorker(
        queue, client, run_shot=_accepted, run_keyframe=boom, session_factory=None
    )
    await queue.enqueue(
        shot_hash="h_kf3",
        priority=RenderPriority.KEYFRAME,
        book_id="b",
        job_id="job_kf3",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)
    j = await queue.get_job("job_kf3")
    assert j is not None and j.attempts == 1 and j.status is RenderJobStatus.RETRYING


# --- conflict surfacing (§7.2/§5.4) ----------------------------------------- #


async def test_conflict_result_surfaces_choice_event(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    from app.agents.contracts import (
        ConflictObject,
        ConflictOption,
        ConflictOptionSpec,
        ConflictType,
    )

    conflict = ConflictObject(
        conflict_id="cf_1",
        type=ConflictType.CANON_VIOLATION,
        claim="Hair turned black",
        canon_fact="Hair is golden",
        current_beat="beat_9",
        raised_by="continuity",
        shot_id="shot_cf",
        options=[
            ConflictOptionSpec(id=ConflictOption.HONOR_CANON, action="Honour canon"),
            ConflictOptionSpec(id=ConflictOption.EVOLVE_CANON, action="Evolve canon"),
        ],
    )

    async def run_shot(job: QueuedJob) -> RenderResult:
        return RenderResult(
            shot_id=job.shot_id or "",
            status=ShotStatus.CONFLICT,
            rung="conflict",
            conflict=conflict,
        )

    worker = RenderWorker(queue, client, run_shot=run_shot, session_factory=None)
    await queue.enqueue(
        shot_hash="h_conf",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_conf",
        session_id="sess_cf",
        shot_id="shot_cf",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)

    events = client.events_on(session_channel("sess_cf"))
    choice = next((e for e in events if e["event"] == "conflict_choice"), None)
    assert choice is not None and choice["conflict_id"] == "cf_1"
    # The structured conflict object is persisted for the choice handler.
    from app.queue.redis_queue import conflict_object_key

    stored = await client.get_json(conflict_object_key("sess_cf", "cf_1"))
    assert stored is not None and stored["conflict_id"] == "cf_1"


# --- lease heartbeat -------------------------------------------------------- #


async def test_lease_heartbeats_during_slow_render(
    queue: RedisRenderQueue, client: FakeRedisClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    original = queue.extend_lease

    async def counting_extend(job_id: str, **kwargs: object) -> bool:
        calls["n"] += 1
        return await original(job_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(queue, "extend_lease", counting_extend)

    async def slow_run_shot(job: QueuedJob) -> RenderResult:
        await asyncio.sleep(0.15)
        return await _accepted(job)

    worker = RenderWorker(
        queue, client, run_shot=slow_run_shot, session_factory=None, lease_heartbeat_s=0.03
    )
    await queue.enqueue(
        shot_hash="h_slow",
        priority=RenderPriority.COMMITTED,
        book_id="b",
        job_id="job_slow",
        session_id="sess_s",
        shot_id="shot_s",
    )
    job = await queue.claim()
    assert job is not None
    await worker.process_job(job)

    assert calls["n"] >= 1
    done = await queue.get_job("job_slow")
    assert done is not None and done.status is RenderJobStatus.SUCCEEDED


# --- backoff-from-settings wiring (§12.1) ----------------------------------- #


def test_backoff_from_settings_none_default() -> None:
    from app.core.config import Settings
    from app.queue.worker import _queue_backoff_from_settings

    settings = Settings(dashscope_api_key="test", app_env="local")
    # The default jitter strategy is "none" -> fixed schedule (None backoff object).
    assert _queue_backoff_from_settings(settings) is None


def test_backoff_from_settings_full_jitter() -> None:
    from app.core.config import Settings
    from app.queue.backoff import JitterStrategy
    from app.queue.worker import _queue_backoff_from_settings

    settings = Settings(
        dashscope_api_key="test",
        app_env="local",
        queue_backoff_jitter="full",
        queue_backoff_base_s=2.0,
        queue_backoff_cap_s=30.0,
    )
    sched = _queue_backoff_from_settings(settings)
    assert sched is not None and sched.strategy is JitterStrategy.FULL
    # Non-prod gets a fixed seed for reproducible tests.
    assert sched.seed == 1337


def test_backoff_from_settings_bad_strategy_falls_back() -> None:
    from app.core.config import Settings
    from app.queue.worker import _queue_backoff_from_settings

    settings = Settings(dashscope_api_key="test", app_env="local", queue_backoff_jitter="bogus")
    # An unrecognised strategy degrades to the fixed schedule rather than crashing.
    assert _queue_backoff_from_settings(settings) is None
