"""A durable, distributed :class:`~app.jobs.store.JobStore` over Redis.

Mirrors the in-memory store's contract with Redis primitives so multiple nodes
share one run store:

* **Run records** are Redis hashes (``{ns}:run:{id}``).
* **The ready set** is a sorted set (``{ns}:ready``) scored by ``available_at``
  (epoch ms); a worker claims the earliest member whose score has arrived.
* **Idempotency** is a string key (``{ns}:key:{idempotency_key}``) holding the
  active run id; :meth:`enqueue` is a Lua check-and-set so two nodes enqueuing the
  same due instant collapse to one run.
* **The claim** is a Lua pop-from-ready-and-lease so two workers never claim the
  same run; leased runs go into a processing sorted set (``{ns}:processing``)
  scored by the lease deadline, which :meth:`reap_expired` scans for crash
  recovery.
* **The DLQ** is a list (``{ns}:dlq``).

Atomicity for the two race-prone paths (enqueue, claim) lives in small Lua
scripts, exactly as the render queue does. Redis is the authoritative store here;
for queryable history + an audit trail use :class:`~app.jobs.db_store.PostgresJobStore`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.jobs.store import EnqueueResult, StoreStats
from app.jobs.types import (
    TERMINAL_RUN_STATUSES,
    JobRun,
    JobRunStatus,
    RunOutcome,
    TriggerKind,
)

# Enqueue iff no active (non-terminal) run already holds the key.
# KEYS = [key_key, run_key, ready_key]
# ARGV = [run_id, fields_json, available_ms]
_ENQUEUE_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
    local st = redis.call('HGET', 'NS' .. ':run:' .. existing, 'status')
    if st and not TERMINAL[st] then
        return {'existing', existing}
    end
end
local fields = cjson.decode(ARGV[2])
for k, v in pairs(fields) do
    redis.call('HSET', KEYS[2], k, v)
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[3], tonumber(ARGV[3]), ARGV[1])
return {'created', ARGV[1]}
"""

# Pop the earliest ready run and lease it into processing.
# KEYS = [ready_key, processing_key]
# ARGV = [now_ms, lease_ms, lease_token, ns]
_CLAIM_LUA = """
local now = tonumber(ARGV[1])
local res = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, 1)
if not res or res[1] == nil then
    return false
end
local run_id = res[1]
redis.call('ZREM', KEYS[1], run_id)
local lease_until = now + tonumber(ARGV[2])
redis.call('ZADD', KEYS[2], lease_until, run_id)
local run_key = ARGV[4] .. ':run:' .. run_id
local attempt = tonumber(redis.call('HGET', run_key, 'attempt') or '0') + 1
redis.call('HSET', run_key,
    'status', 'running',
    'attempt', attempt,
    'lease_token', ARGV[3],
    'lease_until', lease_until,
    'started_ms', now)
return run_id
"""


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _from_ms(ms: str | int | None) -> datetime | None:
    if ms is None or ms == "" or ms == "0" or ms == 0:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


class RedisJobStore:
    """A :class:`JobStore` backed by Redis (durable across processes)."""

    def __init__(self, redis: Any, *, namespace: str = "kinora:jobs") -> None:
        self._redis = getattr(redis, "raw", redis)
        self._ns = namespace
        # Bake the namespace + terminal set into the Lua bodies once.
        terminal = "{" + ",".join(f"['{s.value}']=true" for s in TERMINAL_RUN_STATUSES) + "}"
        self._enqueue_lua = (
            "local TERMINAL = " + terminal + "\n" + _ENQUEUE_LUA.replace("'NS'", f"'{self._ns}'")
        )
        self._claim_lua = _CLAIM_LUA

    # -- key helpers --------------------------------------------------------- #

    def _run_key(self, run_id: str) -> str:
        return f"{self._ns}:run:{run_id}"

    def _key_key(self, idempotency_key: str) -> str:
        return f"{self._ns}:key:{idempotency_key}"

    @property
    def _ready_key(self) -> str:
        return f"{self._ns}:ready"

    @property
    def _processing_key(self) -> str:
        return f"{self._ns}:processing"

    @property
    def _dlq_key(self) -> str:
        return f"{self._ns}:dlq"

    @property
    def _index_key(self) -> str:
        return f"{self._ns}:index"

    def _stat_key(self, name: str) -> str:
        return f"{self._ns}:stat:{name}"

    # -- enqueue ------------------------------------------------------------- #

    async def enqueue(
        self,
        *,
        job_name: str,
        idempotency_key: str,
        scheduled_for: datetime,
        max_attempts: int,
        trigger_kind: TriggerKind,
        payload: Mapping[str, Any] | None = None,
        available_at: datetime | None = None,
    ) -> EnqueueResult:
        run_id = uuid.uuid4().hex
        avail = available_at or scheduled_for
        now = datetime.now(UTC)
        fields: dict[str, str] = {
            "id": run_id,
            "job_name": job_name,
            "idempotency_key": idempotency_key,
            "status": JobRunStatus.PENDING.value,
            "scheduled_ms": str(_to_ms(scheduled_for)),
            "available_ms": str(_to_ms(avail)),
            "created_ms": str(_to_ms(now)),
            "attempt": "0",
            "max_attempts": str(max_attempts),
            "trigger_kind": trigger_kind.value,
            "payload": json.dumps(dict(payload or {}), separators=(",", ":")),
        }
        raw = await self._redis.eval(
            self._enqueue_lua,
            3,
            self._key_key(idempotency_key),
            self._run_key(run_id),
            self._ready_key,
            run_id,
            json.dumps(fields, separators=(",", ":")),
            str(_to_ms(avail)),
        )
        status = raw[0]
        resolved = raw[1]
        if status == "existing":
            run = await self.get(resolved)
            if run is not None:
                return EnqueueResult(run=run, created=False)
            # The active run vanished between check and read; fall through as new.
        await self._redis.sadd(self._index_key, run_id)
        await self._redis.incr(self._stat_key("enqueued_total"))
        run = await self.get(run_id)
        assert run is not None
        return EnqueueResult(run=run, created=True)

    # -- claim --------------------------------------------------------------- #

    async def claim_due(
        self, *, now: datetime, lease_seconds: float, job_names: list[str] | None = None
    ) -> JobRun | None:
        token = uuid.uuid4().hex
        # The Lua claim is name-agnostic; when a name filter is supplied we may
        # have to skip + re-park a claimed run that does not match. Bounded loop.
        skipped: list[tuple[str, int]] = []
        try:
            for _ in range(64):
                run_id = await self._redis.eval(
                    self._claim_lua,
                    2,
                    self._ready_key,
                    self._processing_key,
                    str(_to_ms(now)),
                    str(int(lease_seconds * 1000)),
                    token,
                    self._ns,
                )
                if not run_id:
                    return None
                run = await self.get(run_id)
                if run is None:
                    continue
                if job_names is not None and run.job_name not in job_names:
                    skipped.append((run.id, _to_ms(run.available_at or run.scheduled_for)))
                    continue
                return run
            return None
        finally:
            # Re-park any non-matching runs we popped so another worker can claim.
            for rid, avail_ms in skipped:
                await self._redis.zrem(self._processing_key, rid)
                await self._redis.hset(self._run_key(rid), "status", JobRunStatus.PENDING.value)
                await self._redis.zadd(self._ready_key, {rid: avail_ms})

    # -- terminal / retry transitions --------------------------------------- #

    async def complete(
        self, run_id: str, *, outcome: RunOutcome, detail: Mapping[str, Any]
    ) -> None:
        status = JobRunStatus.SKIPPED if outcome is RunOutcome.SKIPPED else JobRunStatus.SUCCEEDED
        await self._finish(
            run_id,
            status=status,
            outcome=outcome,
            detail=detail,
            clear_key=True,
        )
        await self._redis.incr(self._stat_key("succeeded_total"))

    async def retry(self, run_id: str, *, available_at: datetime, error: str) -> None:
        run = await self.get(run_id)
        if run is None:
            return
        await self._redis.zrem(self._processing_key, run_id)
        await self._redis.hset(
            self._run_key(run_id),
            mapping={
                "status": JobRunStatus.RETRYING.value,
                "available_ms": str(_to_ms(available_at)),
                "error": error[:1000],
                "lease_token": "",
                "lease_until": "0",
            },
        )
        await self._redis.zadd(self._ready_key, {run_id: _to_ms(available_at)})
        await self._redis.incr(self._stat_key("failed_total"))

    async def deadletter(self, run_id: str, *, error: str) -> None:
        await self._finish(
            run_id,
            status=JobRunStatus.DEADLETTER,
            outcome=RunOutcome.FAILED,
            detail={"error": error},
            clear_key=True,
            error=error,
        )
        await self._redis.lpush(self._dlq_key, run_id)
        await self._redis.incr(self._stat_key("failed_total"))
        await self._redis.incr(self._stat_key("deadletter_total"))

    async def cancel(self, run_id: str) -> bool:
        run = await self.get(run_id)
        if run is None or run.is_terminal:
            return False
        await self._finish(
            run_id, status=JobRunStatus.CANCELLED, outcome=None, detail={}, clear_key=True
        )
        return True

    async def _finish(
        self,
        run_id: str,
        *,
        status: JobRunStatus,
        outcome: RunOutcome | None,
        detail: Mapping[str, Any],
        clear_key: bool,
        error: str | None = None,
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            return
        await self._redis.zrem(self._processing_key, run_id)
        await self._redis.zrem(self._ready_key, run_id)
        mapping: dict[str, str] = {
            "status": status.value,
            "finished_ms": str(_to_ms(datetime.now(UTC))),
            "detail": json.dumps(dict(detail), separators=(",", ":")),
            "lease_token": "",
            "lease_until": "0",
        }
        if outcome is not None:
            mapping["outcome"] = outcome.value
        if error is not None:
            mapping["error"] = error[:1000]
        await self._redis.hset(self._run_key(run_id), mapping=mapping)
        if clear_key:
            # Only clear the idempotency key if it still points at this run.
            current = await self._redis.get(self._key_key(run.idempotency_key))
            if current == run_id:
                await self._redis.delete(self._key_key(run.idempotency_key))

    # -- crash recovery ------------------------------------------------------ #

    async def reap_expired(self, *, now: datetime) -> int:
        now_ms = _to_ms(now)
        expired = await self._redis.zrangebyscore(self._processing_key, "-inf", now_ms)
        count = 0
        for run_id in expired:
            run = await self.get(run_id)
            await self._redis.zrem(self._processing_key, run_id)
            if run is None or run.is_terminal:
                continue
            await self._redis.hset(
                self._run_key(run_id),
                mapping={
                    "status": JobRunStatus.RETRYING.value,
                    "available_ms": str(now_ms),
                    "lease_token": "",
                    "lease_until": "0",
                },
            )
            await self._redis.zadd(self._ready_key, {run_id: now_ms})
            count += 1
        return count

    # -- reads --------------------------------------------------------------- #

    async def get(self, run_id: str) -> JobRun | None:
        data = await self._redis.hgetall(self._run_key(run_id))
        if not data or "id" not in data:
            return None
        return self._from_hash(data)

    def _from_hash(self, data: dict[str, str]) -> JobRun:
        outcome_raw = data.get("outcome") or None
        return JobRun(
            id=data["id"],
            job_name=data["job_name"],
            idempotency_key=data["idempotency_key"],
            status=JobRunStatus(data["status"]),
            scheduled_for=_from_ms(data.get("scheduled_ms")) or datetime.now(UTC),
            created_at=_from_ms(data.get("created_ms")) or datetime.now(UTC),
            attempt=int(data.get("attempt", "0") or 0),
            max_attempts=int(data.get("max_attempts", "1") or 1),
            available_at=_from_ms(data.get("available_ms")),
            started_at=_from_ms(data.get("started_ms")),
            finished_at=_from_ms(data.get("finished_ms")),
            outcome=RunOutcome(outcome_raw) if outcome_raw else None,
            error=data.get("error") or None,
            detail=json.loads(data.get("detail") or "{}"),
            payload=json.loads(data.get("payload") or "{}"),
            lease_token=data.get("lease_token") or None,
            lease_until=_from_ms(data.get("lease_until")),
            trigger_kind=TriggerKind(data.get("trigger_kind", TriggerKind.MANUAL.value)),
        )

    async def list_runs(
        self, *, job_name: str | None = None, status: JobRunStatus | None = None, limit: int = 100
    ) -> list[JobRun]:
        ids = await self._redis.smembers(self._index_key)
        runs: list[JobRun] = []
        for rid in ids:
            run = await self.get(rid)
            if run is None:
                continue
            if job_name is not None and run.job_name != job_name:
                continue
            if status is not None and run.status != status:
                continue
            runs.append(run)
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    async def dead_letters(self, *, limit: int = 100) -> list[JobRun]:
        ids = await self._redis.lrange(self._dlq_key, 0, limit - 1)
        runs = [await self.get(rid) for rid in ids]
        return [r for r in runs if r is not None]

    async def stats(self) -> StoreStats:
        ids = await self._redis.smembers(self._index_key)
        by_status: dict[str, int] = {}
        for rid in ids:
            st = await self._redis.hget(self._run_key(rid), "status")
            if st:
                by_status[st] = by_status.get(st, 0) + 1
        return StoreStats(
            by_status=by_status,
            dead_letters=int(await self._redis.llen(self._dlq_key)),
            enqueued_total=await self._stat("enqueued_total"),
            succeeded_total=await self._stat("succeeded_total"),
            failed_total=await self._stat("failed_total"),
            deadletter_total=await self._stat("deadletter_total"),
        )

    async def _stat(self, name: str) -> int:
        value = await self._redis.get(self._stat_key(name))
        return int(value) if value else 0

    async def purge(self) -> None:
        """Delete every key in this store's namespace (test/teardown helper)."""
        cursor = 0
        pattern = f"{self._ns}:*"
        while True:
            cursor, batch = await self._redis.scan(cursor=cursor, match=pattern, count=500)
            if batch:
                await self._redis.delete(*batch)
            if cursor == 0:
                break


__all__ = ["RedisJobStore"]
