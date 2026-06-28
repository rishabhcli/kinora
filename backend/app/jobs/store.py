"""The durable run store — the framework's at-least-once backbone.

A :class:`JobStore` persists :class:`~app.jobs.types.JobRun` records and exposes
the small set of *atomic* operations the scheduler and worker need:

* :meth:`enqueue` — create a run for a (job, due-instant) **iff** no active run
  already exists for its idempotency key (the dedup that makes a double-fire a
  no-op). Returns the run and whether it was newly created.
* :meth:`claim_due` — atomically lease the next run whose ``available_at`` has
  arrived, flipping it to ``running`` with a lease token + deadline so a second
  worker cannot also claim it (the at-least-once lease).
* :meth:`complete` / :meth:`retry` / :meth:`deadletter` / :meth:`cancel` — drive
  a leased run to a terminal/retry state.
* :meth:`reap_expired` — re-queue runs whose worker lease lapsed (crash recovery).
* read helpers (:meth:`get`, :meth:`list_runs`, :meth:`dead_letters`, :meth:`stats`).

:class:`InMemoryJobStore` is the reference implementation (used by the virtual
clock harness and the bulk of the tests); :mod:`app.jobs.redis_store` and
:mod:`app.jobs.db_store` provide the distributed/durable variants behind the same
protocol. The store is the *only* place run lifecycle invariants live, so the
dispatcher/worker stay thin.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from app.jobs.clock import Clock
from app.jobs.types import (
    TERMINAL_RUN_STATUSES,
    JobRun,
    JobRunStatus,
    RunOutcome,
    TriggerKind,
)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Outcome of :meth:`JobStore.enqueue`."""

    run: JobRun
    created: bool  # False => an active run for this key already existed (dedup)


@dataclass(frozen=True, slots=True)
class StoreStats:
    """A point-in-time snapshot of run counts by status + lifetime counters."""

    by_status: dict[str, int]
    dead_letters: int
    enqueued_total: int
    succeeded_total: int
    failed_total: int
    deadletter_total: int

    @property
    def active(self) -> int:
        """Non-terminal runs (pending + running + retrying)."""
        terminal = {s.value for s in TERMINAL_RUN_STATUSES}
        return sum(c for s, c in self.by_status.items() if s not in terminal)


@runtime_checkable
class JobStore(Protocol):
    """Durable, atomic persistence of job runs (the at-least-once backbone)."""

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
    ) -> EnqueueResult: ...

    async def claim_due(
        self, *, now: datetime, lease_seconds: float, job_names: list[str] | None = None
    ) -> JobRun | None: ...

    async def complete(
        self, run_id: str, *, outcome: RunOutcome, detail: Mapping[str, Any]
    ) -> None: ...

    async def retry(
        self, run_id: str, *, available_at: datetime, error: str
    ) -> None: ...

    async def deadletter(self, run_id: str, *, error: str) -> None: ...

    async def cancel(self, run_id: str) -> bool: ...

    async def reap_expired(self, *, now: datetime) -> int: ...

    async def get(self, run_id: str) -> JobRun | None: ...

    async def list_runs(
        self, *, job_name: str | None = None, status: JobRunStatus | None = None, limit: int = 100
    ) -> list[JobRun]: ...

    async def dead_letters(self, *, limit: int = 100) -> list[JobRun]: ...

    async def stats(self) -> StoreStats: ...


class InMemoryJobStore:
    """An in-process reference :class:`JobStore` (great for tests + the harness).

    Backed by a dict guarded by an :class:`asyncio.Lock` so the atomic operations
    behave correctly under concurrent workers on one event loop. Not durable
    across processes — use :class:`~app.jobs.redis_store.RedisJobStore` or
    :class:`~app.jobs.db_store.PostgresJobStore` for that.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock
        self._runs: dict[str, JobRun] = {}
        # idempotency_key -> run_id of the currently-active run for that key.
        self._active_key: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._counters: dict[str, int] = {
            "enqueued_total": 0,
            "succeeded_total": 0,
            "failed_total": 0,
            "deadletter_total": 0,
        }

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        from app.jobs.clock import SystemClock

        return SystemClock().now()

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
        async with self._lock:
            existing_id = self._active_key.get(idempotency_key)
            if existing_id is not None:
                existing = self._runs.get(existing_id)
                if existing is not None and not existing.is_terminal:
                    return EnqueueResult(run=replace(existing), created=False)
            run = JobRun(
                id=uuid.uuid4().hex,
                job_name=job_name,
                idempotency_key=idempotency_key,
                status=JobRunStatus.PENDING,
                scheduled_for=scheduled_for,
                created_at=self._now(),
                attempt=0,
                max_attempts=max_attempts,
                available_at=available_at or scheduled_for,
                payload=dict(payload or {}),
                trigger_kind=trigger_kind,
            )
            self._runs[run.id] = run
            self._active_key[idempotency_key] = run.id
            self._counters["enqueued_total"] += 1
            return EnqueueResult(run=replace(run), created=True)

    async def claim_due(
        self, *, now: datetime, lease_seconds: float, job_names: list[str] | None = None
    ) -> JobRun | None:
        names = set(job_names) if job_names is not None else None
        async with self._lock:
            candidates = [
                r
                for r in self._runs.values()
                if r.status in (JobRunStatus.PENDING, JobRunStatus.RETRYING)
                and (r.available_at is None or r.available_at <= now)
                and (names is None or r.job_name in names)
            ]
            if not candidates:
                return None
            # Earliest available_at, then earliest scheduled_for (FIFO-ish, fair).
            candidates.sort(key=lambda r: (r.available_at or r.scheduled_for, r.scheduled_for))
            run = candidates[0]
            run.status = JobRunStatus.RUNNING
            run.attempt += 1
            run.started_at = now
            run.lease_token = uuid.uuid4().hex
            run.lease_until = now + timedelta(seconds=lease_seconds)
            return replace(run)

    async def complete(
        self, run_id: str, *, outcome: RunOutcome, detail: Mapping[str, Any]
    ) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = (
                JobRunStatus.SKIPPED if outcome is RunOutcome.SKIPPED else JobRunStatus.SUCCEEDED
            )
            run.outcome = outcome
            run.detail = dict(detail)
            run.finished_at = self._now()
            run.lease_token = None
            run.lease_until = None
            self._clear_active(run)
            self._counters["succeeded_total"] += 1

    async def retry(self, run_id: str, *, available_at: datetime, error: str) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = JobRunStatus.RETRYING
            run.error = error
            run.available_at = available_at
            run.lease_token = None
            run.lease_until = None
            self._counters["failed_total"] += 1
            # The key stays active so a retry of the same logical run still dedups.

    async def deadletter(self, run_id: str, *, error: str) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.status = JobRunStatus.DEADLETTER
            run.error = error
            run.outcome = RunOutcome.FAILED
            run.finished_at = self._now()
            run.lease_token = None
            run.lease_until = None
            self._clear_active(run)
            self._counters["failed_total"] += 1
            self._counters["deadletter_total"] += 1

    async def cancel(self, run_id: str) -> bool:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.is_terminal:
                return False
            run.status = JobRunStatus.CANCELLED
            run.finished_at = self._now()
            run.lease_token = None
            run.lease_until = None
            self._clear_active(run)
            return True

    async def reap_expired(self, *, now: datetime) -> int:
        async with self._lock:
            count = 0
            for run in self._runs.values():
                if (
                    run.status is JobRunStatus.RUNNING
                    and run.lease_until is not None
                    and run.lease_until <= now
                ):
                    run.status = JobRunStatus.RETRYING
                    run.available_at = now
                    run.lease_token = None
                    run.lease_until = None
                    count += 1
            return count

    async def get(self, run_id: str) -> JobRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            return replace(run) if run is not None else None

    async def list_runs(
        self, *, job_name: str | None = None, status: JobRunStatus | None = None, limit: int = 100
    ) -> list[JobRun]:
        async with self._lock:
            runs = [
                replace(r)
                for r in self._runs.values()
                if (job_name is None or r.job_name == job_name)
                and (status is None or r.status == status)
            ]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    async def dead_letters(self, *, limit: int = 100) -> list[JobRun]:
        return await self.list_runs(status=JobRunStatus.DEADLETTER, limit=limit)

    async def stats(self) -> StoreStats:
        async with self._lock:
            by_status: dict[str, int] = {}
            for run in self._runs.values():
                by_status[run.status.value] = by_status.get(run.status.value, 0) + 1
            return StoreStats(
                by_status=by_status,
                dead_letters=by_status.get(JobRunStatus.DEADLETTER.value, 0),
                enqueued_total=self._counters["enqueued_total"],
                succeeded_total=self._counters["succeeded_total"],
                failed_total=self._counters["failed_total"],
                deadletter_total=self._counters["deadletter_total"],
            )

    def _clear_active(self, run: JobRun) -> None:
        if self._active_key.get(run.idempotency_key) == run.id:
            del self._active_key[run.idempotency_key]


__all__ = ["EnqueueResult", "InMemoryJobStore", "JobStore", "StoreStats"]
