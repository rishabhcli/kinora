"""Redis-backed priority render queue (kinora.md §12.1–§12.3).

Three lanes over Redis sorted sets — **committed > speculative > keyframe** —
each scored by a *ready-at* millisecond timestamp so that exponential-backoff
retries are just future scores. The design guarantees:

* **Idempotency / dedup (§12.3).** A shot is keyed by its ``shot_hash``: a
  ``shot -> job_id`` index is written atomically on enqueue, so re-enqueuing a
  known/in-flight shot (even from a *different* session) returns the existing
  ``job_id`` and never creates a second render — duplicate Scheduler events can
  never double-spend the video budget.
* **Priority (§4.9/§12.2).** A worker pulls the highest-priority ready lane
  first; committed work is therefore always drained before speculative work.
* **Backpressure (§12.2).** When the total queued depth crosses a threshold,
  *new speculative* enqueues are **dropped** (the keyframe ladder covers them);
  committed enqueues are always admitted.
* **Preemption (§4.9).** A committed enqueue marks an in-flight speculative job
  cancellable so a worker frees its slot cooperatively.
* **Cancellation (§12.1).** Per-trajectory cancel tokens; ``cancel_by_token`` /
  ``cancel_distant`` flip a flag that workers honour at safe points, releasing
  any reserved budget.
* **Retries → DLQ (§12.1).** Transient failures back off (2s, 8s, 30s) up to a
  cap, then the job dead-letters and the shot drops to degradation.

Atomicity for the two race-prone operations (enqueue's check-and-set and claim's
pop-and-lease) is provided by small Lua scripts. Durable job state is optionally
mirrored into the Postgres ``render_jobs`` table via :class:`RenderJobRepo` when
a ``session_factory`` is supplied; Redis remains the authoritative queue
mechanism.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.db.models.enums import RenderJobStatus, RenderPriority
from app.observability import metrics

logger = get_logger("app.queue.redis_queue")

#: Lane priority order — highest first (committed preempts/precedes the rest).
LANE_ORDER: tuple[RenderPriority, ...] = (
    RenderPriority.COMMITTED,
    RenderPriority.SPECULATIVE,
    RenderPriority.KEYFRAME,
)

#: The cheap, preemptible lanes (no video-seconds): speculative + keyframe.
PREEMPTIBLE_LANES: tuple[RenderPriority, ...] = (
    RenderPriority.SPECULATIVE,
    RenderPriority.KEYFRAME,
)

# Statuses past which a job is no longer actionable by a worker.
_TERMINAL = frozenset(
    {
        RenderJobStatus.SUCCEEDED.value,
        RenderJobStatus.CANCELLED.value,
        RenderJobStatus.DEADLETTER.value,
    }
)


def session_channel(session_id: str) -> str:
    """The pub/sub channel a session's generation events fan out on (§5.6)."""
    return f"kinora:events:session:{session_id}"


def book_channel(book_id: str) -> str:
    """The fallback channel for jobs not tied to a live session."""
    return f"kinora:events:book:{book_id}"


# --------------------------------------------------------------------------- #
# Lua scripts (the only operations that must be atomic)
# --------------------------------------------------------------------------- #

# Enqueue: idempotency check, backpressure check, then create. Returns a
# two-element array ``{status, job_id}`` where status is enqueued|existing|dropped.
#   KEYS = [shot_key, job_key, lane_key, lane_committed, lane_spec, lane_keyframe, token_key]
#   ARGV = [job_id, priority, fields_json, backpressure_depth, has_token, ready_at_ms]
_ENQUEUE_LUA = """
local shot_key = KEYS[1]
local job_key = KEYS[2]
local lane_key = KEYS[3]
local lane_c = KEYS[4]
local lane_s = KEYS[5]
local lane_k = KEYS[6]
local token_key = KEYS[7]
local job_id = ARGV[1]
local priority = ARGV[2]
local fields_json = ARGV[3]
local threshold = tonumber(ARGV[4])
local has_token = ARGV[5]
local ready_at = tonumber(ARGV[6])

local existing = redis.call('GET', shot_key)
if existing then
    return {'existing', existing}
end

if priority == 'speculative' then
    local depth = redis.call('ZCARD', lane_c) + redis.call('ZCARD', lane_s)
        + redis.call('ZCARD', lane_k)
    if depth >= threshold then
        return {'dropped', ''}
    end
end

local fields = cjson.decode(fields_json)
for k, v in pairs(fields) do
    redis.call('HSET', job_key, k, v)
end
redis.call('SET', shot_key, job_id)
redis.call('ZADD', lane_key, ready_at, job_id)
if has_token == '1' then
    redis.call('SADD', token_key, job_id)
end
return {'enqueued', job_id}
"""

# Claim: scan lanes in priority order for the earliest ready job, pop it, and
# lease it into the processing set. Returns the job_id or false.
#   KEYS = [lane_1, ..., lane_n, processing]
#   ARGV = [now_ms, lease_ms]
_CLAIM_LUA = """
local n = #KEYS
local processing = KEYS[n]
local now = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
for i = 1, n - 1 do
    local lane = KEYS[i]
    local res = redis.call('ZRANGEBYSCORE', lane, '-inf', now, 'LIMIT', 0, 1)
    if res and res[1] ~= nil then
        local job_id = res[1]
        redis.call('ZREM', lane, job_id)
        redis.call('ZADD', processing, now + lease, job_id)
        return job_id
    end
end
return false
"""


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #


class EnqueueStatus(StrEnum):
    """The outcome of an enqueue attempt."""

    ENQUEUED = "enqueued"
    EXISTING = "existing"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """The result of :meth:`RedisRenderQueue.enqueue`."""

    status: EnqueueStatus
    job_id: str | None

    @property
    def admitted(self) -> bool:
        """True when a job is now queued or already known (not dropped)."""
        return self.status is not EnqueueStatus.DROPPED

    @property
    def created(self) -> bool:
        """True only when this call created a brand-new job."""
        return self.status is EnqueueStatus.ENQUEUED


class RetryDecision(StrEnum):
    """What :meth:`RedisRenderQueue.retry` decided to do with a failed job."""

    RETRY = "retry"
    DEADLETTER = "deadletter"


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """The result of a retry decision (delay in seconds when re-queued)."""

    decision: RetryDecision
    attempts: int
    delay_s: float = 0.0


@dataclass(slots=True)
class QueuedJob:
    """A render job as stored in Redis (one hash per job)."""

    id: str
    shot_hash: str
    priority: RenderPriority
    status: RenderJobStatus
    book_id: str
    attempts: int = 0
    session_id: str | None = None
    shot_id: str | None = None
    beat_id: str | None = None
    scene_id: str | None = None
    cancel_token: str | None = None
    reservation_id: str | None = None
    reserved_video_s: float = 0.0
    target_duration_s: float = 5.0
    target_word: int = 0
    prompt: str | None = None
    cancelled: bool = False
    provider_task_id: str | None = None
    error: str | None = None

    @classmethod
    def from_hash(cls, data: dict[str, Any]) -> QueuedJob:
        """Parse a Redis hash (all-string values) into a typed job."""

        def _opt(key: str) -> str | None:
            value = data.get(key)
            return value if value not in (None, "") else None

        return cls(
            id=data["id"],
            shot_hash=data["shot_hash"],
            priority=RenderPriority(data["priority"]),
            status=RenderJobStatus(data.get("status", RenderJobStatus.QUEUED.value)),
            book_id=data.get("book_id", ""),
            attempts=int(data.get("attempts", "0") or 0),
            session_id=_opt("session_id"),
            shot_id=_opt("shot_id"),
            beat_id=_opt("beat_id"),
            scene_id=_opt("scene_id"),
            cancel_token=_opt("cancel_token"),
            reservation_id=_opt("reservation_id"),
            reserved_video_s=float(data.get("reserved_video_s", "0") or 0.0),
            target_duration_s=float(data.get("target_duration_s", "5") or 5.0),
            target_word=int(float(data.get("target_word", "0") or 0)),
            prompt=_opt("prompt"),
            cancelled=data.get("cancelled", "0") == "1",
            provider_task_id=_opt("provider_task_id"),
            error=_opt("error"),
        )


@dataclass(frozen=True, slots=True)
class QueueStats:
    """A point-in-time snapshot of queue depths and lifetime counters."""

    depths: dict[str, int]
    processing: int
    dlq: int
    enqueued_total: int
    succeeded_total: int
    dropped_total: int
    deadletter_total: int
    cancelled_total: int

    @property
    def total_queued(self) -> int:
        """Sum of all lane depths (excludes in-flight/processing)."""
        return sum(self.depths.values())


# A factory yielding an ``async with`` DB session (e.g. ``app.db.session.get_session``).
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


class RedisRenderQueue:
    """A three-lane priority queue over Redis with idempotency and cancellation."""

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:rq",
        backpressure_depth: int = 64,
        # The worker lease must exceed the *whole* render window or the reaper can
        # reclaim a job that is still rendering and a second worker re-submits it —
        # double video spend under live video (§12.1). A committed render polls Wan
        # up to providers.video poll timeout (600s) plus QA + the degrade ladder, so
        # the default lease is 15 min (> that window). The worker *also* heartbeats
        # the lease while actively rendering (``extend_lease``), so this is only the
        # ceiling for a missed heartbeat, not the steady-state guarantee.
        lease_ms: int = 900_000,
        retry_cap: int = 2,
        retry_backoff_s: Sequence[float] = (2.0, 8.0, 30.0),
        success_ttl_s: int = 3600,
        session_factory: SessionFactory | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        # ``redis`` may be a RedisClient wrapper or a raw redis.asyncio client.
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._backpressure_depth = backpressure_depth
        self._lease_ms = lease_ms
        self._retry_cap = retry_cap
        self._backoff = tuple(retry_backoff_s)
        self._success_ttl_s = success_ttl_s
        self._session_factory = session_factory
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    # -- key helpers --------------------------------------------------------- #

    def _lane_key(self, priority: RenderPriority) -> str:
        return f"{self._ns}:lane:{priority.value}"

    def _job_key(self, job_id: str) -> str:
        return f"{self._ns}:job:{job_id}"

    def _shot_key(self, shot_hash: str) -> str:
        return f"{self._ns}:shot:{shot_hash}"

    def _token_key(self, token: str) -> str:
        return f"{self._ns}:token:{token}"

    def _inflight_key(self, priority: RenderPriority) -> str:
        return f"{self._ns}:inflight:{priority.value}"

    @property
    def _processing_key(self) -> str:
        return f"{self._ns}:processing"

    @property
    def _dlq_key(self) -> str:
        return f"{self._ns}:dlq"

    def _stat_key(self, name: str) -> str:
        return f"{self._ns}:stat:{name}"

    def _now(self, now_ms: int | None) -> int:
        return self._clock_ms() if now_ms is None else now_ms

    # -- enqueue ------------------------------------------------------------- #

    async def enqueue(
        self,
        *,
        shot_hash: str,
        priority: RenderPriority,
        book_id: str,
        job_id: str,
        session_id: str | None = None,
        shot_id: str | None = None,
        beat_id: str | None = None,
        scene_id: str | None = None,
        cancel_token: str | None = None,
        reservation_id: str | None = None,
        reserved_video_s: float = 0.0,
        target_duration_s: float = 5.0,
        target_word: int = 0,
        prompt: str | None = None,
        now_ms: int | None = None,
    ) -> EnqueueResult:
        """Atomically admit a job (or return the existing one / drop it).

        Idempotent on ``shot_hash``: a known shot returns its existing
        ``job_id``. Speculative jobs are dropped under backpressure. A committed
        enqueue preempts one in-flight speculative job.
        """
        now = self._now(now_ms)
        fields: dict[str, str] = {
            "id": job_id,
            "shot_hash": shot_hash,
            "priority": priority.value,
            "status": RenderJobStatus.QUEUED.value,
            "book_id": book_id,
            "attempts": "0",
            "reserved_video_s": _num(reserved_video_s),
            "target_duration_s": _num(target_duration_s),
            "target_word": str(int(target_word)),
            "cancelled": "0",
            "created_ms": str(now),
            "ready_at": str(now),
        }
        _put_optional(fields, "session_id", session_id)
        _put_optional(fields, "shot_id", shot_id)
        _put_optional(fields, "beat_id", beat_id)
        _put_optional(fields, "scene_id", scene_id)
        _put_optional(fields, "cancel_token", cancel_token)
        _put_optional(fields, "reservation_id", reservation_id)
        _put_optional(fields, "prompt", prompt)

        keys = [
            self._shot_key(shot_hash),
            self._job_key(job_id),
            self._lane_key(priority),
            self._lane_key(RenderPriority.COMMITTED),
            self._lane_key(RenderPriority.SPECULATIVE),
            self._lane_key(RenderPriority.KEYFRAME),
            self._token_key(cancel_token or "__none__"),
        ]
        raw = await self._redis.eval(
            _ENQUEUE_LUA,
            len(keys),
            *keys,
            job_id,
            priority.value,
            json.dumps(fields, separators=(",", ":")),
            str(self._backpressure_depth),
            "1" if cancel_token else "0",
            str(now),
        )
        status = EnqueueStatus(raw[0])
        resolved = raw[1] or None

        if status is EnqueueStatus.DROPPED:
            await self._redis.incr(self._stat_key("dropped_total"))
            metrics.inc_job("dropped")
            logger.info("queue.dropped", shot_hash=shot_hash, priority=priority.value)
            return EnqueueResult(status=status, job_id=None)

        if status is EnqueueStatus.EXISTING:
            logger.info("queue.dedup", shot_hash=shot_hash, job_id=resolved)
            return EnqueueResult(status=status, job_id=resolved)

        await self._redis.incr(self._stat_key("enqueued_total"))
        metrics.inc_job("enqueued")
        if priority is RenderPriority.COMMITTED:
            await self._preempt_speculative()
        await self._mirror_create(
            job_id=job_id,
            priority=priority,
            session_id=session_id,
            shot_id=shot_id,
            shot_hash=shot_hash,
            cancel_token=cancel_token,
            reserved_video_s=reserved_video_s,
        )
        logger.info(
            "queue.enqueued",
            job_id=job_id,
            shot_hash=shot_hash,
            priority=priority.value,
            session_id=session_id,
        )
        return EnqueueResult(status=status, job_id=job_id)

    async def _preempt_speculative(self) -> str | None:
        """Mark one in-flight speculative job cancellable so committed can run (§4.9)."""
        members = await self._redis.smembers(self._inflight_key(RenderPriority.SPECULATIVE))
        for victim in members:
            data = await self._redis.hgetall(self._job_key(victim))
            if data and data.get("status") not in _TERMINAL and data.get("cancelled") != "1":
                await self._redis.hset(self._job_key(victim), "cancelled", "1")
                await self._redis.hset(self._job_key(victim), "preempted", "1")
                logger.info("queue.preempt", job_id=victim)
                return str(victim)
        return None

    # -- claim --------------------------------------------------------------- #

    async def claim(
        self,
        *,
        lanes: Sequence[RenderPriority] | None = None,
        now_ms: int | None = None,
    ) -> QueuedJob | None:
        """Pop the earliest *ready* job from the highest-priority lane and lease it.

        ``lanes`` restricts the claim to specific lanes (a worker pool dedicated
        to one lane); the default scans all lanes in priority order.
        """
        now = self._now(now_ms)
        order = list(lanes) if lanes is not None else list(LANE_ORDER)
        keys = [self._lane_key(p) for p in order] + [self._processing_key]
        job_id = await self._redis.eval(_CLAIM_LUA, len(keys), *keys, str(now), str(self._lease_ms))
        if not job_id:
            return None
        job_key = self._job_key(job_id)
        await self._redis.hset(
            job_key,
            mapping={
                "status": RenderJobStatus.RESERVED.value,
                "lease_until": str(now + self._lease_ms),
            },
        )
        priority = RenderPriority(await self._redis.hget(job_key, "priority"))
        await self._redis.sadd(self._inflight_key(priority), job_id)
        job = await self.get_job(str(job_id))
        await self._mirror_status(str(job_id), RenderJobStatus.RESERVED)
        return job

    async def mark_submitted(
        self, job_id: str, *, provider_task_id: str | None = None
    ) -> None:
        """Transition a claimed job to ``submitted`` (render started)."""
        mapping: dict[str, str] = {"status": RenderJobStatus.SUBMITTED.value}
        if provider_task_id is not None:
            mapping["provider_task_id"] = provider_task_id
        await self._redis.hset(self._job_key(job_id), mapping=mapping)
        await self._mirror_status(
            job_id, RenderJobStatus.SUBMITTED, provider_task_id=provider_task_id
        )

    # -- ack / retry / cancel finalize --------------------------------------- #

    async def ack(self, job_id: str) -> None:
        """Mark a job succeeded and clear it from the in-flight structures."""
        job = await self.get_job(job_id)
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.hset(self._job_key(job_id), "status", RenderJobStatus.SUCCEEDED.value)
        if job is not None:
            await self._redis.srem(self._inflight_key(job.priority), job_id)
            if job.cancel_token:
                await self._redis.srem(self._token_key(job.cancel_token), job_id)
            # Keep the idempotency index briefly so duplicate events still dedup,
            # then let it expire (the shot cache is the durable dedup after that).
            await self._redis.expire(self._shot_key(job.shot_hash), self._success_ttl_s)
        await self._redis.incr(self._stat_key("succeeded_total"))
        metrics.inc_job("succeeded")
        await self._mirror_status(job_id, RenderJobStatus.SUCCEEDED)

    async def retry(
        self, job_id: str, *, error: str | None = None, now_ms: int | None = None
    ) -> RetryOutcome:
        """Back off and re-queue a transiently failed job, or dead-letter it."""
        now = self._now(now_ms)
        job = await self.get_job(job_id)
        if job is None:
            return RetryOutcome(decision=RetryDecision.DEADLETTER, attempts=0)
        attempts = job.attempts + 1
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.srem(self._inflight_key(job.priority), job_id)
        job_key = self._job_key(job_id)
        mapping: dict[str, str] = {"attempts": str(attempts)}
        if error is not None:
            mapping["error"] = error[:500]

        if attempts > self._retry_cap:
            mapping["status"] = RenderJobStatus.DEADLETTER.value
            await self._redis.hset(job_key, mapping=mapping)
            await self._redis.lpush(self._dlq_key, job_id)
            await self._redis.delete(self._shot_key(job.shot_hash))
            if job.cancel_token:
                await self._redis.srem(self._token_key(job.cancel_token), job_id)
            await self._redis.incr(self._stat_key("deadletter_total"))
            metrics.inc_job("deadletter")
            metrics.inc_dlq()
            logger.warning("queue.deadletter", job_id=job_id, attempts=attempts, error=error)
            await self._mirror_status(job_id, RenderJobStatus.DEADLETTER, attempts=attempts)
            return RetryOutcome(decision=RetryDecision.DEADLETTER, attempts=attempts)

        delay_s = self._backoff[min(attempts - 1, len(self._backoff) - 1)]
        ready_at = now + int(delay_s * 1000)
        mapping["status"] = RenderJobStatus.RETRYING.value
        mapping["ready_at"] = str(ready_at)
        await self._redis.hset(job_key, mapping=mapping)
        await self._redis.zadd(self._lane_key(job.priority), {job_id: ready_at})
        metrics.inc_job("retrying")
        logger.info("queue.retry", job_id=job_id, attempts=attempts, delay_s=delay_s)
        await self._mirror_status(job_id, RenderJobStatus.RETRYING, attempts=attempts)
        return RetryOutcome(decision=RetryDecision.RETRY, attempts=attempts, delay_s=delay_s)

    async def finalize_cancelled(self, job_id: str) -> None:
        """Finalize a job a worker drained as cancelled (budget already released)."""
        job = await self.get_job(job_id)
        await self._redis.zrem(self._processing_key, job_id)
        await self._redis.hset(self._job_key(job_id), "status", RenderJobStatus.CANCELLED.value)
        if job is not None:
            await self._redis.srem(self._inflight_key(job.priority), job_id)
            await self._redis.delete(self._shot_key(job.shot_hash))
            if job.cancel_token:
                await self._redis.srem(self._token_key(job.cancel_token), job_id)
        await self._redis.incr(self._stat_key("cancelled_total"))
        metrics.inc_job("cancelled")
        metrics.inc_cancellations()
        await self._mirror_status(job_id, RenderJobStatus.CANCELLED)

    # -- cancellation -------------------------------------------------------- #

    async def cancel_by_token(
        self, token: str, *, lanes: Sequence[RenderPriority] | None = None
    ) -> int:
        """Flag every non-terminal job on ``token`` (optionally lane-scoped).

        Committed jobs are preserved when ``lanes`` excludes them — this is what
        lets idle-pause halt *speculative* work while freezing the committed
        buffer (§4.7).
        """
        lane_filter = {p.value for p in lanes} if lanes is not None else None
        members = await self._redis.smembers(self._token_key(token))
        count = 0
        for job_id in members:
            data = await self._redis.hgetall(self._job_key(job_id))
            if not data or data.get("status") in _TERMINAL:
                continue
            if lane_filter is not None and data.get("priority") not in lane_filter:
                continue
            await self._redis.hset(self._job_key(job_id), "cancelled", "1")
            count += 1
        if count:
            logger.info("queue.cancel_token", token=token, count=count)
        return count

    async def cancel_distant(
        self,
        token: str,
        *,
        focus_word: int,
        velocity_wps: float,
        threshold_s: float = 120.0,
        lanes: Sequence[RenderPriority] = PREEMPTIBLE_LANES,
    ) -> int:
        """Flag in-flight speculative jobs now > ``threshold_s`` reading-time away (§4.8).

        Committed jobs near the new position are never cancelled.
        """
        lane_filter = {p.value for p in lanes}
        v = max(abs(velocity_wps), 0.1)
        members = await self._redis.smembers(self._token_key(token))
        count = 0
        for job_id in members:
            data = await self._redis.hgetall(self._job_key(job_id))
            if not data or data.get("status") in _TERMINAL:
                continue
            if data.get("priority") not in lane_filter:
                continue
            target = float(data.get("target_word", "0") or 0.0)
            eta = abs(target - focus_word) / v
            if eta > threshold_s:
                await self._redis.hset(self._job_key(job_id), "cancelled", "1")
                count += 1
        if count:
            logger.info("queue.cancel_distant", token=token, count=count, focus_word=focus_word)
        return count

    async def mark_cancelled(self, job_id: str) -> None:
        """Set a job's cooperative cancel flag (used for direct/seek cancels)."""
        await self._redis.hset(self._job_key(job_id), "cancelled", "1")

    async def is_cancelled(self, job_id: str) -> bool:
        """Whether a job has been flagged for cooperative cancellation."""
        flag = await self._redis.hget(self._job_key(job_id), "cancelled")
        return flag == "1"

    # -- reads --------------------------------------------------------------- #

    async def get_job(self, job_id: str) -> QueuedJob | None:
        """Load a job's full record (or ``None`` if it has been purged)."""
        data = await self._redis.hgetall(self._job_key(job_id))
        if not data or "id" not in data:
            return None
        return QueuedJob.from_hash(data)

    async def lookup(self, shot_hash: str) -> str | None:
        """Return the job_id currently indexed for ``shot_hash`` (idempotency probe)."""
        return await self._redis.get(self._shot_key(shot_hash))

    async def depth(self, priority: RenderPriority | None = None) -> int:
        """Queued depth for one lane, or the total across all lanes."""
        if priority is not None:
            return int(await self._redis.zcard(self._lane_key(priority)))
        total = 0
        for p in LANE_ORDER:
            total += int(await self._redis.zcard(self._lane_key(p)))
        return total

    async def inflight(self, priority: RenderPriority) -> int:
        """Number of leased (in-flight) jobs in a lane."""
        return int(await self._redis.scard(self._inflight_key(priority)))

    async def dlq_len(self) -> int:
        """Number of dead-lettered jobs."""
        return int(await self._redis.llen(self._dlq_key))

    async def stats(self) -> QueueStats:
        """A snapshot of lane depths plus lifetime counters.

        Doubles as the refresh point for the live ``queue_depth`` gauge so a
        scrape (or the worker's periodic snapshot) reflects current depth.
        """
        depths = {p.value: await self.depth(p) for p in LANE_ORDER}
        for lane, depth in depths.items():
            metrics.set_queue_depth(lane, depth)
        return QueueStats(
            depths=depths,
            processing=int(await self._redis.zcard(self._processing_key)),
            dlq=await self.dlq_len(),
            enqueued_total=await self._stat(self._stat_key("enqueued_total")),
            succeeded_total=await self._stat(self._stat_key("succeeded_total")),
            dropped_total=await self._stat(self._stat_key("dropped_total")),
            deadletter_total=await self._stat(self._stat_key("deadletter_total")),
            cancelled_total=await self._stat(self._stat_key("cancelled_total")),
        )

    async def _stat(self, key: str) -> int:
        value = await self._redis.get(key)
        return int(value) if value else 0

    # -- lease recovery ------------------------------------------------------ #

    async def extend_lease(
        self, job_id: str, *, now_ms: int | None = None, lease_ms: int | None = None
    ) -> bool:
        """Heartbeat: push a leased job's processing deadline out by one lease.

        Re-scores the job in the processing set to ``now + lease`` so a render that
        outlasts the original lease is not reaped + re-claimed mid-flight (which
        would double-submit it and double-spend video, §12.1). Returns ``False``
        when the job is no longer leased (acked/cancelled/never claimed), so a late
        heartbeat after completion is a harmless no-op.
        """
        now = self._now(now_ms)
        lease = self._lease_ms if lease_ms is None else lease_ms
        if await self._redis.zscore(self._processing_key, job_id) is None:
            return False
        await self._redis.zadd(self._processing_key, {job_id: now + lease})
        await self._redis.hset(self._job_key(job_id), "lease_until", str(now + lease))
        return True

    async def reap_expired(self, *, now_ms: int | None = None) -> int:
        """Re-queue jobs whose worker lease expired (crash recovery)."""
        now = self._now(now_ms)
        expired = await self._redis.zrangebyscore(self._processing_key, "-inf", now)
        count = 0
        for job_id in expired:
            job = await self.get_job(job_id)
            if job is None or job.status.value in _TERMINAL:
                await self._redis.zrem(self._processing_key, job_id)
                continue
            await self._redis.zrem(self._processing_key, job_id)
            await self._redis.srem(self._inflight_key(job.priority), job_id)
            await self._redis.zadd(self._lane_key(job.priority), {job_id: now})
            await self._redis.hset(self._job_key(job_id), "status", RenderJobStatus.QUEUED.value)
            count += 1
        if count:
            logger.info("queue.reaped", count=count)
        return count

    # -- Postgres mirror (durable job state, optional) ----------------------- #

    async def _mirror_create(
        self,
        *,
        job_id: str,
        priority: RenderPriority,
        session_id: str | None,
        shot_id: str | None,
        shot_hash: str,
        cancel_token: str | None,
        reserved_video_s: float,
    ) -> None:
        if self._session_factory is None:
            return
        from app.db.repositories.render_job import RenderJobRepo

        try:
            async with self._session_factory() as db:
                await RenderJobRepo(db).create(
                    priority=priority,
                    session_id=session_id,
                    shot_id=shot_id,
                    shot_hash=shot_hash,
                    cancel_token=cancel_token,
                    reserved_video_s=reserved_video_s,
                    job_id=job_id,
                )
        except Exception as exc:  # durability mirror must never break the queue
            logger.warning("queue.mirror_create_failed", job_id=job_id, error=str(exc))

    async def _mirror_status(
        self,
        job_id: str,
        status: RenderJobStatus,
        *,
        attempts: int | None = None,
        provider_task_id: str | None = None,
    ) -> None:
        if self._session_factory is None:
            return
        from app.db.repositories.render_job import RenderJobRepo

        fields: dict[str, Any] = {"status": status}
        if attempts is not None:
            fields["attempts"] = attempts
        if provider_task_id is not None:
            fields["provider_task_id"] = provider_task_id
        try:
            async with self._session_factory() as db:
                await RenderJobRepo(db).update(job_id, **fields)
        except Exception as exc:
            logger.warning("queue.mirror_status_failed", job_id=job_id, error=str(exc))

    # -- maintenance --------------------------------------------------------- #

    async def purge(self) -> None:
        """Delete every key in this queue's namespace (test/teardown helper)."""
        cursor = 0
        pattern = f"{self._ns}:*"
        while True:
            cursor, batch = await self._redis.scan(cursor=cursor, match=pattern, count=500)
            if batch:
                await self._redis.delete(*batch)
            if cursor == 0:
                break


def _num(value: float) -> str:
    """Render a float compactly (integers without a trailing ``.0``)."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _put_optional(fields: dict[str, str], key: str, value: str | None) -> None:
    if value is not None and value != "":
        fields[key] = value


async def iter_events(
    redis: Any, channel: str, *, timeout: float = 5.0
) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON events from ``channel`` until the timeout elapses.

    A thin helper over the :class:`app.redis.client.RedisClient` pub/sub surface
    for tests and the API SSE bridge (Phase 9).
    """
    async with redis.subscribe(channel) as pubsub:
        while True:
            message = await redis.next_message(pubsub, timeout=timeout)
            if message is None:
                return
            yield message


__all__ = [
    "LANE_ORDER",
    "PREEMPTIBLE_LANES",
    "EnqueueResult",
    "EnqueueStatus",
    "QueueStats",
    "QueuedJob",
    "RedisRenderQueue",
    "RetryDecision",
    "RetryOutcome",
    "book_channel",
    "iter_events",
    "session_channel",
]
