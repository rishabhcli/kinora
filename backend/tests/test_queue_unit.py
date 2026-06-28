"""Full priority-queue behaviour, exercised with no infra (app.queue.fakeredis).

This mirrors ``test_queue_redis.py`` (which needs a live ``KINORA_TEST_REDIS_URL``
and otherwise skips) but runs the *real* :class:`RedisRenderQueue` against the
in-process :class:`FakeAsyncRedis`. The queue logic — Lua-driven enqueue/claim,
sorted-set lanes, idempotency, preemption, backpressure, cancellation, the
backoff→DLQ retry path, and lease/reaper recovery — is identical; only the Redis
backend is swapped. Deterministic ``now_ms`` makes the time-based assertions exact.

Covers kinora.md §12.1–§12.3, §4.8, §4.9.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.fakeredis import FakeRedisClient
from app.queue.redis_queue import EnqueueStatus, RedisRenderQueue, RetryDecision

_BASE_MS = 1_700_000_000_000


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def queue(client: FakeRedisClient) -> RedisRenderQueue:
    return RedisRenderQueue(
        client,
        namespace="kinora:test:rq",
        backpressure_depth=3,
        retry_cap=2,
        retry_backoff_s=(2.0, 8.0, 30.0),
        lease_ms=1000,
    )


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
        job_id=kw.pop("job_id", shot_hash),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# --- idempotency / dedup (§12.3) -------------------------------------------- #


async def test_idempotency_same_shot_hash_returns_same_job(queue: RedisRenderQueue) -> None:
    first = await _enqueue(queue, "hash_A", job_id="job_1")
    second = await queue.enqueue(
        shot_hash="hash_A", priority=RenderPriority.COMMITTED, book_id="b", job_id="job_2"
    )
    assert first.status is EnqueueStatus.ENQUEUED and first.job_id == "job_1"  # type: ignore[attr-defined]
    assert second.status is EnqueueStatus.EXISTING and second.job_id == "job_1"
    assert await queue.depth(RenderPriority.COMMITTED) == 1


async def test_cross_session_dedup(queue: RedisRenderQueue) -> None:
    a = await _enqueue(queue, "shared", job_id="jA", session_id="sess_A")
    b = await _enqueue(queue, "shared", job_id="jB", session_id="sess_B")
    assert a.created and a.job_id == "jA"  # type: ignore[attr-defined]
    assert b.status is EnqueueStatus.EXISTING and b.job_id == "jA"  # type: ignore[attr-defined]
    assert await queue.depth() == 1  # paid for once, not twice


async def test_lookup_returns_indexed_job(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "probe", job_id="jp")
    assert await queue.lookup("probe") == "jp"
    assert await queue.lookup("absent") is None


async def test_cancel_token_set_carries_ttl(client: FakeRedisClient) -> None:
    q = RedisRenderQueue(client, namespace="ns", token_ttl_s=120)
    await _enqueue(q, "ht", RenderPriority.SPECULATIVE, job_id="jt", cancel_token="traj_1")
    # The token set exists and is members={jt}; the fake set an expiry deadline.
    assert await client.raw.smembers(q._token_key("traj_1")) == {"jt"}
    assert q._token_key("traj_1") in client.raw._expiry


# --- priority (§4.9/§12.2) -------------------------------------------------- #


async def test_priority_committed_before_speculative_before_keyframe(
    queue: RedisRenderQueue,
) -> None:
    await _enqueue(queue, "spec1", RenderPriority.SPECULATIVE, job_id="js")
    await _enqueue(queue, "comm1", RenderPriority.COMMITTED, job_id="jc")
    await _enqueue(queue, "kf1", RenderPriority.KEYFRAME, job_id="jk")
    first = await queue.claim()
    second = await queue.claim()
    third = await queue.claim()
    assert first is not None and first.id == "jc"
    assert second is not None and second.id == "js"
    assert third is not None and third.id == "jk"


async def test_claim_can_be_lane_scoped(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c", RenderPriority.COMMITTED, job_id="c")
    await _enqueue(queue, "s", RenderPriority.SPECULATIVE, job_id="s")
    # A keyframe-only pool never claims the committed/speculative jobs.
    assert await queue.claim(lanes=[RenderPriority.KEYFRAME]) is None
    only_spec = await queue.claim(lanes=[RenderPriority.SPECULATIVE])
    assert only_spec is not None and only_spec.id == "s"


async def test_claim_returns_none_when_empty(queue: RedisRenderQueue) -> None:
    assert await queue.claim() is None


# --- backpressure (§12.2) --------------------------------------------------- #


async def test_backpressure_drops_speculative_admits_committed(queue: RedisRenderQueue) -> None:
    for i in range(3):  # cap == 3
        res = await _enqueue(queue, f"s{i}", RenderPriority.SPECULATIVE, job_id=f"s{i}")
        assert res.created  # type: ignore[attr-defined]
    dropped = await _enqueue(queue, "s_over", RenderPriority.SPECULATIVE, job_id="s_over")
    admitted = await _enqueue(queue, "c_always", RenderPriority.COMMITTED, job_id="c_always")
    assert dropped.status is EnqueueStatus.DROPPED and dropped.job_id is None  # type: ignore[attr-defined]
    assert admitted.created  # type: ignore[attr-defined]
    stats = await queue.stats()
    assert stats.dropped_total == 1


async def test_keyframe_admitted_even_at_backpressure(queue: RedisRenderQueue) -> None:
    # Backpressure only gates *speculative*; keyframe + committed always admitted.
    for i in range(5):
        await _enqueue(queue, f"c{i}", RenderPriority.COMMITTED, job_id=f"c{i}")
    kf = await _enqueue(queue, "kf", RenderPriority.KEYFRAME, job_id="kf")
    assert kf.created  # type: ignore[attr-defined]


# --- preemption (§4.9) ------------------------------------------------------ #


async def test_committed_enqueue_preempts_inflight_speculative(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "spec", RenderPriority.SPECULATIVE, job_id="spec")
    claimed = await queue.claim(lanes=[RenderPriority.SPECULATIVE])
    assert claimed is not None and claimed.id == "spec"
    assert not await queue.is_cancelled("spec")
    await _enqueue(queue, "comm", RenderPriority.COMMITTED, job_id="comm")
    assert await queue.is_cancelled("spec") is True


async def test_preempt_skips_already_cancelled_victim(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "spec", RenderPriority.SPECULATIVE, job_id="spec")
    claimed = await queue.claim(lanes=[RenderPriority.SPECULATIVE])
    assert claimed is not None
    await queue.mark_cancelled("spec")
    # A second committed enqueue finds no fresh victim (already flagged); no crash.
    await _enqueue(queue, "comm", RenderPriority.COMMITTED, job_id="comm")
    assert await queue.is_cancelled("spec") is True


# --- cancellation (§4.7/§4.8/§12.1) ----------------------------------------- #


async def test_cancel_by_token_is_lane_scoped(queue: RedisRenderQueue) -> None:
    token = "traj_x"
    await _enqueue(queue, "c", RenderPriority.COMMITTED, job_id="c", cancel_token=token)
    await _enqueue(queue, "s", RenderPriority.SPECULATIVE, job_id="s", cancel_token=token)
    await _enqueue(queue, "k", RenderPriority.KEYFRAME, job_id="k", cancel_token=token)
    count = await queue.cancel_by_token(
        token, lanes=[RenderPriority.SPECULATIVE, RenderPriority.KEYFRAME]
    )
    assert count == 2
    assert await queue.is_cancelled("s") and await queue.is_cancelled("k")
    assert await queue.is_cancelled("c") is False  # committed frozen, not cancelled


async def test_cancel_by_token_all_lanes(queue: RedisRenderQueue) -> None:
    token = "traj_all"
    await _enqueue(queue, "c", RenderPriority.COMMITTED, job_id="c", cancel_token=token)
    await _enqueue(queue, "s", RenderPriority.SPECULATIVE, job_id="s", cancel_token=token)
    assert await queue.cancel_by_token(token) == 2


async def test_cancel_distant_cancels_far_speculative_only(queue: RedisRenderQueue) -> None:
    token = "traj_seek"
    await _enqueue(
        queue,
        "near",
        RenderPriority.SPECULATIVE,
        job_id="near",
        cancel_token=token,
        target_word=200,
    )
    await _enqueue(
        queue,
        "far",
        RenderPriority.SPECULATIVE,
        job_id="far",
        cancel_token=token,
        target_word=100_000,
    )
    cancelled = await queue.cancel_distant(token, focus_word=0, velocity_wps=4.0, threshold_s=120.0)
    assert cancelled == 1
    assert await queue.is_cancelled("far") is True
    assert await queue.is_cancelled("near") is False


async def test_cancel_distant_ignores_committed_near_new_position(queue: RedisRenderQueue) -> None:
    token = "traj_seek2"
    # A committed job far away is *not* cancelled — cancel_distant is speculative-only.
    await _enqueue(
        queue,
        "cfar",
        RenderPriority.COMMITTED,
        job_id="cfar",
        cancel_token=token,
        target_word=999_999,
    )
    cancelled = await queue.cancel_distant(token, focus_word=0, velocity_wps=4.0)
    assert cancelled == 0
    assert await queue.is_cancelled("cfar") is False


async def test_finalize_cancelled_clears_indexes(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c", RenderPriority.SPECULATIVE, job_id="c", cancel_token="t")
    claimed = await queue.claim()
    assert claimed is not None
    await queue.finalize_cancelled("c")
    job = await queue.get_job("c")
    assert job is not None and job.status is RenderJobStatus.CANCELLED
    assert await queue.lookup("c") is None  # idempotency index cleared
    assert await queue.inflight(RenderPriority.SPECULATIVE) == 0
    stats = await queue.stats()
    assert stats.cancelled_total == 1


# --- claim / submit lifecycle ----------------------------------------------- #


async def test_claim_leases_and_tracks_inflight(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c", RenderPriority.COMMITTED, job_id="c", now_ms=_BASE_MS)
    job = await queue.claim(now_ms=_BASE_MS)
    assert job is not None and job.status is RenderJobStatus.RESERVED
    assert await queue.inflight(RenderPriority.COMMITTED) == 1


async def test_mark_submitted_records_provider_task(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c", job_id="c")
    await queue.claim()
    await queue.mark_submitted("c", provider_task_id="task-123")
    job = await queue.get_job("c")
    assert job is not None
    assert job.status is RenderJobStatus.SUBMITTED and job.provider_task_id == "task-123"


async def test_ack_succeeds_and_clears_inflight(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c", job_id="c", cancel_token="tok")
    await queue.claim()
    await queue.ack("c")
    job = await queue.get_job("c")
    assert job is not None and job.status is RenderJobStatus.SUCCEEDED
    assert await queue.inflight(RenderPriority.COMMITTED) == 0
    # The token member is released on ack.
    assert await queue._redis.smembers(queue._token_key("tok")) == set()
    stats = await queue.stats()
    assert stats.succeeded_total == 1


# --- retries → DLQ (§12.1) -------------------------------------------------- #


async def test_retry_backoff_then_deadletter(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "flaky", job_id="flaky", now_ms=_BASE_MS)
    assert await queue.claim(now_ms=_BASE_MS) is not None

    out1 = await queue.retry("flaky", error="503", now_ms=_BASE_MS)
    assert out1.decision is RetryDecision.RETRY and out1.attempts == 1 and out1.delay_s == 2.0
    assert await queue.claim(now_ms=_BASE_MS + 1_900) is None  # not ready before +2s
    assert await queue.claim(now_ms=_BASE_MS + 2_000) is not None

    out2 = await queue.retry("flaky", error="timeout", now_ms=_BASE_MS + 2_000)
    assert out2.decision is RetryDecision.RETRY and out2.attempts == 2 and out2.delay_s == 8.0
    assert await queue.claim(now_ms=_BASE_MS + 10_000) is not None

    out3 = await queue.retry("flaky", error="boom", now_ms=_BASE_MS + 10_000)
    assert out3.decision is RetryDecision.DEADLETTER and out3.attempts == 3

    job = await queue.get_job("flaky")
    assert job is not None and job.status is RenderJobStatus.DEADLETTER
    assert await queue.dlq_len() == 1
    assert await queue.lookup("flaky") is None  # index cleared on DLQ
    stats = await queue.stats()
    assert stats.deadletter_total == 1


async def test_retry_missing_job_deadletters_noop(queue: RedisRenderQueue) -> None:
    out = await queue.retry("ghost")
    assert out.decision is RetryDecision.DEADLETTER and out.attempts == 0


async def test_far_future_retry_forces_immediate_deadletter(queue: RedisRenderQueue) -> None:
    # A permanent error retried with now_ms in the far future exhausts backoff
    # immediately on the *first* attempt path the worker uses for _PERMANENT.
    await _enqueue(queue, "perm", job_id="perm", now_ms=_BASE_MS)
    await queue.claim(now_ms=_BASE_MS)
    # First retry still RETRY (cap=2); two more exhaust it. This proves the
    # backoff schedule is independent of the (huge) now_ms value.
    await queue.retry("perm", now_ms=2**62)
    await queue.retry("perm", now_ms=2**62)
    out = await queue.retry("perm", now_ms=2**62)
    assert out.decision is RetryDecision.DEADLETTER


# --- lease / reaper recovery (§12.1) ---------------------------------------- #


async def test_extend_lease_prevents_premature_reap(queue: RedisRenderQueue) -> None:
    base = _BASE_MS
    await _enqueue(queue, "slow", job_id="slow", now_ms=base)
    assert await queue.claim(now_ms=base) is not None  # leased until base + 1000

    assert await queue.extend_lease("slow", now_ms=base + 500) is True  # -> base + 1500
    assert await queue.reap_expired(now_ms=base + 1001) == 0  # original lease held off
    assert await queue.reap_expired(now_ms=base + 1501) == 1  # extended lease lapsed
    assert await queue.extend_lease("ghost", now_ms=base) is False  # not leased


async def test_reap_requeues_to_same_lane(queue: RedisRenderQueue) -> None:
    base = _BASE_MS
    await _enqueue(queue, "s", RenderPriority.SPECULATIVE, job_id="s", now_ms=base)
    await queue.claim(now_ms=base)
    assert await queue.inflight(RenderPriority.SPECULATIVE) == 1
    reaped = await queue.reap_expired(now_ms=base + 2000)
    assert reaped == 1
    assert await queue.inflight(RenderPriority.SPECULATIVE) == 0
    assert await queue.depth(RenderPriority.SPECULATIVE) == 1  # back in its lane
    job = await queue.get_job("s")
    assert job is not None and job.status is RenderJobStatus.QUEUED


async def test_reap_skips_terminal_jobs(queue: RedisRenderQueue) -> None:
    base = _BASE_MS
    await _enqueue(queue, "c", job_id="c", now_ms=base)
    await queue.claim(now_ms=base)
    await queue.ack("c")  # succeeded but still scored in processing until reaped
    # A terminal job past its lease is dropped from processing, never re-queued.
    assert await queue.reap_expired(now_ms=base + 5000) == 0
    assert await queue.depth(RenderPriority.COMMITTED) == 0


# --- stats / depth ---------------------------------------------------------- #


async def test_depth_and_stats_snapshot(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c1", job_id="c1")
    await _enqueue(queue, "c2", job_id="c2")
    await _enqueue(queue, "s1", RenderPriority.SPECULATIVE, job_id="s1")
    claimed = await queue.claim(lanes=[RenderPriority.COMMITTED])
    assert claimed is not None
    await queue.ack(claimed.id)
    stats = await queue.stats()
    assert stats.depths[RenderPriority.COMMITTED.value] == 1
    assert stats.depths[RenderPriority.SPECULATIVE.value] == 1
    assert stats.enqueued_total == 3 and stats.succeeded_total == 1
    assert stats.total_queued == 2 and await queue.depth() == 2


async def test_purge_clears_namespace(queue: RedisRenderQueue) -> None:
    await _enqueue(queue, "c1", job_id="c1")
    await _enqueue(queue, "s1", RenderPriority.SPECULATIVE, job_id="s1")
    await queue.purge()
    assert await queue.depth() == 0
    assert await queue.get_job("c1") is None


# --- jittered backoff wiring (§12.1) ---------------------------------------- #


async def test_queue_uses_jittered_backoff_schedule(client: FakeRedisClient) -> None:
    from app.queue.backoff import BackoffSchedule, JitterStrategy

    # A seeded full-jitter schedule is materialised into the RetryPolicy, so the
    # re-queue delays are bounded by the exponential envelope and reproducible.
    backoff = BackoffSchedule(strategy=JitterStrategy.FULL, base_s=2.0, cap_s=30.0, seed=99)
    q = RedisRenderQueue(
        client, namespace="kinora:test:jit", retry_cap=3, backoff=backoff, lease_ms=1000
    )
    await q.enqueue(
        shot_hash="jx", priority=RenderPriority.COMMITTED, book_id="b", job_id="jx", now_ms=_BASE_MS
    )
    await q.claim(now_ms=_BASE_MS)
    out1 = await q.retry("jx", now_ms=_BASE_MS)
    out2 = await q.retry("jx", now_ms=_BASE_MS)
    # Attempt 1's delay is uniform(0, 2); attempt 2's is uniform(0, 4): jittered,
    # within the exponential envelope, never the fixed 2/8/30 literal.
    assert 0.0 <= out1.delay_s <= 2.0
    assert 0.0 <= out2.delay_s <= 4.0


async def test_queue_backoff_none_keeps_fixed_schedule(client: FakeRedisClient) -> None:
    q = RedisRenderQueue(
        client,
        namespace="kinora:test:fix",
        retry_cap=3,
        retry_backoff_s=(2.0, 8.0, 30.0),
        lease_ms=1000,
    )
    await q.enqueue(
        shot_hash="fx", priority=RenderPriority.COMMITTED, book_id="b", job_id="fx", now_ms=_BASE_MS
    )
    await q.claim(now_ms=_BASE_MS)
    out1 = await q.retry("fx", now_ms=_BASE_MS)
    assert out1.delay_s == 2.0  # exact fixed schedule, no jitter
