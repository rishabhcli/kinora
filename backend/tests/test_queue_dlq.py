"""Dead-letter inspect + replay tooling tests (app.queue.dlq, kinora.md §12.1).

Drives the real queue's DLQ path against the in-process fake: fail a job past its
retry cap so it dead-letters, then inspect / stat / replay / purge it.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.dlq import DeadLetterQueue, _error_class
from app.queue.fakeredis import FakeRedisClient
from app.queue.redis_queue import EnqueueStatus, RedisRenderQueue

_BASE = 1_700_000_000_000


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


@pytest.fixture
def queue(client: FakeRedisClient) -> RedisRenderQueue:
    return RedisRenderQueue(
        client, namespace="kinora:test:dlq", retry_cap=1, retry_backoff_s=(0.0,)
    )


async def _deadletter(
    queue: RedisRenderQueue,
    shot_hash: str,
    *,
    job_id: str,
    error: str,
    priority: RenderPriority = RenderPriority.COMMITTED,
) -> None:
    """Enqueue, claim, and fail a job past the cap (=1) so it dead-letters."""
    await queue.enqueue(
        shot_hash=shot_hash,
        priority=priority,
        book_id="b",
        job_id=job_id,
        shot_id=f"shot_{job_id}",
        now_ms=_BASE,
    )
    await queue.claim(now_ms=_BASE)
    await queue.retry(job_id, error=error, now_ms=_BASE)  # attempt 1 -> retry
    await queue.claim(now_ms=_BASE)
    await queue.retry(job_id, error=error, now_ms=_BASE)  # attempt 2 > cap -> DLQ


# --- error-class bucketing -------------------------------------------------- #


def test_error_class_buckets() -> None:
    assert _error_class("503 Service Unavailable") == "503"
    assert _error_class("RuntimeError: boom") == "RuntimeError"
    assert _error_class(None) == "unknown"
    assert _error_class("") == "unknown"


# --- inspect / peek / stats ------------------------------------------------- #


async def test_inspect_returns_deadlettered_jobs(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="503 boom")
    assert await dlq.length() == 1
    entries = await dlq.inspect()
    assert len(entries) == 1
    assert entries[0].job_id == "j1"
    assert entries[0].error == "503 boom"
    assert entries[0].job.status is RenderJobStatus.DEADLETTER


async def test_peek_returns_newest(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="e1")
    await _deadletter(queue, "h2", job_id="j2", error="e2")
    head = await dlq.peek()
    assert head is not None and head.job_id == "j2"  # LPUSH puts newest at head


async def test_stats_histograms(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="503 a")
    await _deadletter(queue, "h2", job_id="j2", error="503 b")
    await _deadletter(
        queue, "h3", job_id="j3", error="RuntimeError x", priority=RenderPriority.SPECULATIVE
    )
    stats = await dlq.stats()
    assert stats.total == 3 and not stats.empty
    assert stats.by_error_class == {"503": 2, "RuntimeError": 1}
    assert stats.by_priority == {"committed": 2, "speculative": 1}


async def test_empty_stats(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    stats = await dlq.stats()
    assert stats.empty and stats.total == 0
    assert await dlq.peek() is None


# --- replay ----------------------------------------------------------------- #


async def test_replay_reenqueues_into_original_lane(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="503", priority=RenderPriority.SPECULATIVE)
    result = await dlq.replay("j1", now_ms=_BASE + 100)
    assert result.status is EnqueueStatus.ENQUEUED
    assert await dlq.length() == 0  # removed from the DLQ
    # The replayed job sits in the speculative lane, attempts reset to 0.
    assert await queue.depth(RenderPriority.SPECULATIVE) == 1
    replayed = await queue.get_job(result.job_id or "")
    assert replayed is not None and replayed.attempts == 0
    assert replayed.shot_hash == "h1"


async def test_replay_is_idempotent_on_shot_hash(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="503")
    first = await dlq.replay("j1")
    # A live job now indexes shot_hash h1; a second replay attempt (job re-added by
    # a racing operator) collapses to the existing job rather than double-queueing.
    await queue._redis.lpush(queue._dlq_key, "j1")  # simulate a re-push
    second = await dlq.replay("j1")
    assert second.status is EnqueueStatus.EXISTING
    assert second.job_id == first.job_id


async def test_replay_missing_job_is_noop(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    result = await dlq.replay("ghost")
    assert result.status is EnqueueStatus.DROPPED and result.job_id is None


async def test_replay_all(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="e")
    await _deadletter(queue, "h2", job_id="j2", error="e")
    results = await dlq.replay_all(now_ms=_BASE + 100)
    assert len(results) == 2
    assert all(r.status is EnqueueStatus.ENQUEUED for r in results)
    assert await dlq.length() == 0
    assert await queue.depth(RenderPriority.COMMITTED) == 2


# --- discard / purge -------------------------------------------------------- #


async def test_discard_drops_without_replay(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="e")
    assert await dlq.discard("j1") is True
    assert await dlq.length() == 0
    assert await queue.depth() == 0  # not re-enqueued
    assert await dlq.discard("j1") is False  # already gone


async def test_purge_clears_all(queue: RedisRenderQueue) -> None:
    dlq = DeadLetterQueue(queue)
    await _deadletter(queue, "h1", job_id="j1", error="e")
    await _deadletter(queue, "h2", job_id="j2", error="e")
    assert await dlq.purge() == 2
    assert await dlq.length() == 0
