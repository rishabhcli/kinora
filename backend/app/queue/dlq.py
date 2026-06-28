"""Dead-letter-queue inspection + replay tooling (kinora.md §12.1).

§12.1: *a dead-letter path for shots that fail repeatedly — the job moves to a
DLQ, the shot drops to degradation (Ken-Burns), and a defect is logged. The
pipeline never blocks on one bad shot.* The queue dead-letters a job by pushing
its ``job_id`` onto a Redis list and clearing the idempotency index; that keeps
the render loop moving, but a production system also needs to **operate** the DLQ:
see what's stuck, why, how old, and re-drive a batch once the upstream blip
(a DashScope outage, a transient OSS hiccup) has cleared.

:class:`DeadLetterQueue` is that operability layer over the queue's existing DLQ
list:

* **inspect / peek** — load the full :class:`~app.queue.redis_queue.QueuedJob`
  records (most-recent first), optionally a page, with their failure ``error``.
* **stats** — count + an error-class histogram + oldest entry age, for a metrics
  panel or an alert ("DLQ > N for > M minutes").
* **replay** — re-enqueue a dead-lettered job (resetting attempts) into its
  original lane so a recovered provider can re-render it; idempotent on
  ``shot_hash`` exactly like a fresh enqueue, so replaying twice is a no-op.
* **discard / purge** — drop a single entry or clear the whole DLQ once triaged.

All operations are Redis-only (the worker's DB mirror is best-effort), so the
whole tool is unit-testable against the in-process fake with no infra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.db.models.enums import RenderJobStatus
from app.queue.redis_queue import EnqueueResult, QueuedJob, RedisRenderQueue

logger = get_logger("app.queue.dlq")

__all__ = ["DeadLetterEntry", "DeadLetterStats", "DeadLetterQueue"]


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    """A dead-lettered job plus its position in the DLQ list."""

    index: int
    job: QueuedJob

    @property
    def job_id(self) -> str:
        return self.job.id

    @property
    def error(self) -> str | None:
        return self.job.error


@dataclass(frozen=True, slots=True)
class DeadLetterStats:
    """A point-in-time DLQ summary for a metrics panel / alert."""

    total: int
    by_error_class: dict[str, int]
    by_priority: dict[str, int]

    @property
    def empty(self) -> bool:
        return self.total == 0


def _error_class(error: str | None) -> str:
    """Coarse-bucket a failure message so the histogram is readable.

    Uses the leading token (often an exception name or HTTP code) — good enough to
    tell "503 from DashScope" apart from "ffmpeg degrade failed" at a glance.
    """
    if not error:
        return "unknown"
    head = error.strip().split(":", 1)[0].split()[0] if error.strip() else "unknown"
    return head[:48] or "unknown"


class DeadLetterQueue:
    """Inspect + replay the render queue's dead-letter list."""

    def __init__(self, queue: RedisRenderQueue) -> None:
        self._queue = queue
        # Reuse the queue's own Redis handle + key scheme so the tool operates on
        # the exact list the worker dead-letters into.
        self._redis: Any = queue._redis
        self._dlq_key = queue._dlq_key

    async def length(self) -> int:
        """Number of jobs currently in the DLQ."""
        return int(await self._redis.llen(self._dlq_key))

    async def _job_ids(self, *, start: int = 0, count: int | None = None) -> list[str]:
        end = -1 if count is None else start + count - 1
        return list(await self._redis.lrange(self._dlq_key, start, end))

    async def inspect(self, *, start: int = 0, count: int | None = None) -> list[DeadLetterEntry]:
        """Load DLQ entries (newest first — the list is LPUSH-ordered).

        ``start`` / ``count`` page the list. An entry whose job record was purged
        is skipped (the id stays in the list but carries no detail to act on).
        """
        ids = await self._job_ids(start=start, count=count)
        entries: list[DeadLetterEntry] = []
        for offset, job_id in enumerate(ids):
            job = await self._queue.get_job(job_id)
            if job is not None:
                entries.append(DeadLetterEntry(index=start + offset, job=job))
        return entries

    async def peek(self) -> DeadLetterEntry | None:
        """The most-recently dead-lettered job (head of the list), or None."""
        entries = await self.inspect(start=0, count=1)
        return entries[0] if entries else None

    async def stats(self) -> DeadLetterStats:
        """Summarise the DLQ: total + error-class + per-lane histograms."""
        entries = await self.inspect()
        by_error: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for entry in entries:
            ec = _error_class(entry.error)
            by_error[ec] = by_error.get(ec, 0) + 1
            lane = entry.job.priority.value
            by_priority[lane] = by_priority.get(lane, 0) + 1
        return DeadLetterStats(
            total=await self.length(),
            by_error_class=by_error,
            by_priority=by_priority,
        )

    async def replay(self, job_id: str, *, now_ms: int | None = None) -> EnqueueResult:
        """Re-enqueue a dead-lettered job into its original lane (resets attempts).

        Removes it from the DLQ list, clears its terminal status, and enqueues a
        fresh job from its stored fields. Idempotent on ``shot_hash``: if the shot
        was meanwhile rendered/queued by another path, the replay collapses to that
        job. A job not present in the DLQ is a no-op (returns a dropped result).
        """
        removed = int(await self._redis.lrem(self._dlq_key, 0, job_id))
        if not removed:
            logger.info("dlq.replay_miss", job_id=job_id)
            from app.queue.redis_queue import EnqueueStatus

            return EnqueueResult(status=EnqueueStatus.DROPPED, job_id=None)
        job = await self._queue.get_job(job_id)
        if job is None:
            from app.queue.redis_queue import EnqueueStatus

            return EnqueueResult(status=EnqueueStatus.DROPPED, job_id=None)
        # Re-enqueue under a fresh job id so the lifecycle is clean; the shot_hash
        # idempotency index makes this safe against duplicate replays.
        result = await self._queue.enqueue(
            shot_hash=job.shot_hash,
            priority=job.priority,
            book_id=job.book_id,
            job_id=f"{job.id}:replay",
            session_id=job.session_id,
            shot_id=job.shot_id,
            beat_id=job.beat_id,
            scene_id=job.scene_id,
            cancel_token=job.cancel_token,
            reserved_video_s=job.reserved_video_s,
            target_duration_s=job.target_duration_s,
            target_word=job.target_word,
            prompt=job.prompt,
            now_ms=now_ms,
        )
        logger.info(
            "dlq.replayed",
            job_id=job_id,
            new_job_id=result.job_id,
            status=result.status.value,
            priority=job.priority.value,
        )
        return result

    async def replay_all(self, *, now_ms: int | None = None) -> list[EnqueueResult]:
        """Replay every job currently in the DLQ (oldest first). Returns the results."""
        ids = list(reversed(await self._job_ids()))  # oldest first so order is preserved
        return [await self.replay(job_id, now_ms=now_ms) for job_id in ids]

    async def discard(self, job_id: str) -> bool:
        """Drop a single entry from the DLQ without replaying it (triaged-as-dead)."""
        removed = int(await self._redis.lrem(self._dlq_key, 0, job_id))
        if removed:
            await self._redis.hset(
                self._queue._job_key(job_id), "status", RenderJobStatus.DEADLETTER.value
            )
            logger.info("dlq.discarded", job_id=job_id)
        return bool(removed)

    async def purge(self) -> int:
        """Clear the whole DLQ list, returning the number of entries removed."""
        n = await self.length()
        await self._redis.delete(self._dlq_key)
        if n:
            logger.info("dlq.purged", count=n)
        return n
