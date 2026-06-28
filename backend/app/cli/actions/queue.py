"""Render-queue inspection + DLQ operations (kinora.md §12.1–§12.3).

The render queue is a three-lane Redis priority queue with idempotency, retries,
and a dead-letter path. These actions give an operator the controls §12.1 calls
for without poking Redis by hand:

* ``stats``       — lane depths, in-flight, DLQ size, lifetime counters.
* ``inspect``     — one job's full record by id.
* ``dlq``         — list the dead-lettered job ids (and their records).
* ``replay``      — re-enqueue a dead-lettered job (clearing its DLQ entry).
* ``purge-dlq``   — clear the dead-letter list (after triage).
* ``reap``        — re-queue jobs whose worker lease expired (crash recovery).
* ``cancel``      — flip the cooperative cancel flag for every job on a token.

All re-enqueues go through the queue's own ``enqueue`` so idempotency/dedup and
the Postgres mirror are honoured exactly as in the live path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cli.errors import not_found
from app.cli.formatting import truncate
from app.cli.output import Payload, Table, kv_table
from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.redis_queue import QueuedJob, RedisRenderQueue


@dataclass(frozen=True, slots=True)
class QueueStatsReport:
    """The result of ``queue stats`` — depths + lifetime counters."""

    depths: dict[str, int]
    processing: int
    dlq: int
    inflight: dict[str, int]
    enqueued_total: int
    succeeded_total: int
    dropped_total: int
    deadletter_total: int
    cancelled_total: int

    @property
    def total_queued(self) -> int:
        return sum(self.depths.values())

    def render_payload(self) -> Payload:
        data = {
            "depths": self.depths,
            "total_queued": self.total_queued,
            "processing": self.processing,
            "inflight": self.inflight,
            "dlq": self.dlq,
            "counters": {
                "enqueued": self.enqueued_total,
                "succeeded": self.succeeded_total,
                "dropped": self.dropped_total,
                "deadletter": self.deadletter_total,
                "cancelled": self.cancelled_total,
            },
        }
        lanes = Table(
            title="lane depths",
            columns=("lane", "queued", "inflight"),
            rows=[
                (lane, str(depth), str(self.inflight.get(lane, 0)))
                for lane, depth in self.depths.items()
            ],
        )
        totals = kv_table(
            "queue totals",
            {
                "total_queued": self.total_queued,
                "processing": self.processing,
                "dlq": self.dlq,
                "enqueued_total": self.enqueued_total,
                "succeeded_total": self.succeeded_total,
                "dropped_total": self.dropped_total,
                "deadletter_total": self.deadletter_total,
                "cancelled_total": self.cancelled_total,
            },
        )
        return Payload.of(data, lanes, totals)


def _job_dict(job: QueuedJob) -> dict[str, object]:
    return {
        "id": job.id,
        "shot_hash": job.shot_hash,
        "priority": job.priority.value,
        "status": job.status.value,
        "book_id": job.book_id,
        "session_id": job.session_id,
        "shot_id": job.shot_id,
        "beat_id": job.beat_id,
        "scene_id": job.scene_id,
        "attempts": job.attempts,
        "cancel_token": job.cancel_token,
        "cancelled": job.cancelled,
        "reserved_video_s": job.reserved_video_s,
        "target_duration_s": job.target_duration_s,
        "provider_task_id": job.provider_task_id,
        "error": job.error,
    }


@dataclass(frozen=True, slots=True)
class JobDetail:
    """The result of ``queue inspect`` — one job's full record."""

    job: QueuedJob

    def render_payload(self) -> Payload:
        data = _job_dict(self.job)
        table = kv_table(f"render job {self.job.id}", dict(data))
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class DlqList:
    """The result of ``queue dlq`` — the dead-lettered jobs."""

    jobs: tuple[QueuedJob, ...]
    job_ids: tuple[str, ...]

    def render_payload(self) -> Payload:
        data = {
            "count": len(self.job_ids),
            "job_ids": list(self.job_ids),
            "jobs": [_job_dict(j) for j in self.jobs],
        }
        table = Table(
            title=f"dead-letter queue ({len(self.job_ids)})",
            columns=("job_id", "priority", "attempts", "shot_hash", "error"),
            rows=[
                (
                    j.id,
                    j.priority.value,
                    str(j.attempts),
                    truncate(j.shot_hash, 16),
                    truncate(j.error, 40) if j.error else "-",
                )
                for j in self.jobs
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class OpResult:
    """A generic queue-operation outcome with structured detail."""

    ok: bool
    action: str
    detail: dict[str, object] = field(default_factory=dict)
    message: str = ""

    def render_payload(self) -> Payload:
        data = {"ok": self.ok, "action": self.action, **self.detail}
        table = kv_table(f"queue {self.action}", {"result": self.message, **self.detail})
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def queue_stats(queue: RedisRenderQueue) -> QueueStatsReport:
    """Snapshot lane depths, in-flight counts, DLQ size, and lifetime counters."""
    stats = await queue.stats()
    inflight = {p.value: await queue.inflight(p) for p in RenderPriority}
    return QueueStatsReport(
        depths=dict(stats.depths),
        processing=stats.processing,
        dlq=stats.dlq,
        inflight=inflight,
        enqueued_total=stats.enqueued_total,
        succeeded_total=stats.succeeded_total,
        dropped_total=stats.dropped_total,
        deadletter_total=stats.deadletter_total,
        cancelled_total=stats.cancelled_total,
    )


async def inspect_job(queue: RedisRenderQueue, job_id: str) -> JobDetail:
    """Load one job's full record (raises not-found if purged/unknown)."""
    job = await queue.get_job(job_id)
    if job is None:
        raise not_found("render job", job_id)
    return JobDetail(job=job)


async def list_dlq(queue: RedisRenderQueue, *, limit: int = 100) -> DlqList:
    """List dead-lettered job ids (newest first) and their loadable records."""
    # The DLQ is a Redis list pushed with LPUSH (newest at head).
    raw = await queue._redis.lrange(queue._dlq_key, 0, limit - 1)  # noqa: SLF001
    job_ids = tuple(str(j) for j in raw)
    jobs: list[QueuedJob] = []
    for jid in job_ids:
        job = await queue.get_job(jid)
        if job is not None:
            jobs.append(job)
    return DlqList(jobs=tuple(jobs), job_ids=job_ids)


async def replay_job(queue: RedisRenderQueue, job_id: str) -> OpResult:
    """Re-enqueue a dead-lettered job through the normal enqueue path.

    Re-uses the job's stored ``shot_hash``/``book_id``/priority so idempotency
    and the DB mirror behave exactly as a fresh enqueue. The DLQ entry is removed
    on success; a job that is no longer dead-lettered is reported untouched.
    """
    job = await queue.get_job(job_id)
    if job is None:
        raise not_found("render job", job_id)
    if job.status is not RenderJobStatus.DEADLETTER:
        return OpResult(
            ok=False,
            action="replay",
            detail={"job_id": job_id, "status": job.status.value},
            message=f"job {job_id} is {job.status.value}, not dead-lettered; left untouched",
        )
    # Remove from the DLQ list, then re-enqueue under a fresh job id.
    await queue._redis.lrem(queue._dlq_key, 0, job_id)  # noqa: SLF001
    # Drop the stale shot index so the re-enqueue is admitted (not deduped to the
    # dead job) — the DLQ path already deleted it, but be defensive.
    await queue._redis.delete(queue._shot_key(job.shot_hash))  # noqa: SLF001
    from app.db.base import new_id

    new_job_id = new_id()
    result = await queue.enqueue(
        shot_hash=job.shot_hash,
        priority=job.priority,
        book_id=job.book_id,
        job_id=new_job_id,
        session_id=job.session_id,
        shot_id=job.shot_id,
        beat_id=job.beat_id,
        scene_id=job.scene_id,
        cancel_token=job.cancel_token,
        reservation_id=job.reservation_id,
        reserved_video_s=job.reserved_video_s,
        target_duration_s=job.target_duration_s,
        target_word=job.target_word,
        prompt=job.prompt,
    )
    return OpResult(
        ok=result.admitted,
        action="replay",
        detail={
            "old_job_id": job_id,
            "new_job_id": result.job_id,
            "status": result.status.value,
            "priority": job.priority.value,
        },
        message=f"replayed {job_id} -> {result.job_id} ({result.status.value})",
    )


async def purge_dlq(queue: RedisRenderQueue) -> OpResult:
    """Clear the dead-letter list entirely (after triage)."""
    count = await queue.dlq_len()
    await queue._redis.delete(queue._dlq_key)  # noqa: SLF001
    return OpResult(
        ok=True,
        action="purge-dlq",
        detail={"removed": count},
        message=f"purged {count} dead-lettered job id(s)",
    )


async def reap_expired(queue: RedisRenderQueue) -> OpResult:
    """Re-queue jobs whose worker lease expired (crash recovery, §12.1)."""
    count = await queue.reap_expired()
    return OpResult(
        ok=True,
        action="reap",
        detail={"reaped": count},
        message=f"re-queued {count} job(s) with expired leases",
    )


async def cancel_token(
    queue: RedisRenderQueue, token: str, *, lanes: list[RenderPriority] | None = None
) -> OpResult:
    """Flip the cooperative cancel flag for every non-terminal job on a token."""
    count = await queue.cancel_by_token(token, lanes=lanes)
    return OpResult(
        ok=True,
        action="cancel",
        detail={"token": token, "cancelled": count},
        message=f"flagged {count} job(s) on token {token} for cancellation",
    )


__all__ = [
    "DlqList",
    "JobDetail",
    "OpResult",
    "QueueStatsReport",
    "cancel_token",
    "inspect_job",
    "list_dlq",
    "purge_dlq",
    "queue_stats",
    "reap_expired",
    "replay_job",
]
