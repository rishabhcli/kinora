"""A durable, queryable :class:`~app.jobs.store.JobStore` over Postgres.

Backs the framework with the ``job_runs`` table (and ``scheduled_jobs`` for
schedule bookkeeping), giving an *auditable* run history alongside the at-least-once
guarantees. The two race-prone paths are made safe by the database itself:

* **Idempotent enqueue.** A partial unique index on ``job_runs.idempotency_key``
  for *active* statuses (pending/running/retrying) means a second enqueue for the
  same due instant raises an ``IntegrityError``; we catch it and return the
  existing active run — the same dedup the in-memory/Redis stores give.
* **Exclusive claim.** ``claim_due`` flips the earliest due, claimable row to
  ``running`` under ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers
  never claim the same row (Postgres' row-lock is the lease's teeth).

Each public method runs in its own short unit of work via an injected committing
session factory (the same shape as :func:`app.db.session.get_session`), so the
store composes with the rest of the backend without holding a session open across
handler execution. Naming note: the *value type* is :class:`app.jobs.types.JobRun`;
the *ORM row* is :class:`app.db.models.job.JobRun` — imported here as ``JobRunRow``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.job import JobRun as JobRunRow
from app.jobs.store import EnqueueResult, StoreStats
from app.jobs.types import (
    JobRun,
    JobRunStatus,
    RunOutcome,
    TriggerKind,
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

#: Active (claimable / in-flight) statuses — the ones the partial unique index covers.
_ACTIVE = (JobRunStatus.PENDING, JobRunStatus.RETRYING, JobRunStatus.RUNNING)
_CLAIMABLE = (JobRunStatus.PENDING, JobRunStatus.RETRYING)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _row_to_run(row: JobRunRow) -> JobRun:
    return JobRun(
        id=row.id,
        job_name=row.job_name,
        idempotency_key=row.idempotency_key,
        status=row.status,
        scheduled_for=_aware(row.scheduled_for) or datetime.now(UTC),
        created_at=_aware(row.created_at) or datetime.now(UTC),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        available_at=_aware(row.available_at),
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at),
        outcome=row.outcome,
        error=row.error,
        detail=dict(row.detail or {}),
        payload=dict(row.payload or {}),
        lease_token=row.lease_token,
        lease_until=_aware(row.lease_until),
        trigger_kind=row.trigger_kind,
    )


class PostgresJobStore:
    """A durable :class:`JobStore` over the ``job_runs`` table."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

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
        avail = available_at or scheduled_for
        # Fast path: return an already-active run for this key.
        existing = await self._active_for_key(idempotency_key)
        if existing is not None:
            return EnqueueResult(run=existing, created=False)
        row = JobRunRow(
            id=uuid.uuid4().hex,
            job_name=job_name,
            idempotency_key=idempotency_key,
            status=JobRunStatus.PENDING,
            trigger_kind=trigger_kind,
            attempt=0,
            max_attempts=max_attempts,
            scheduled_for=scheduled_for,
            available_at=avail,
            detail={},
            payload=dict(payload or {}),
        )
        try:
            async with self._sf() as db:
                db.add(row)
                await db.flush()
                created = _row_to_run(row)
            return EnqueueResult(run=created, created=True)
        except IntegrityError:
            # Lost the race to the partial unique index — return the winner.
            current = await self._active_for_key(idempotency_key)
            if current is not None:
                return EnqueueResult(run=current, created=False)
            raise

    async def _active_for_key(self, idempotency_key: str) -> JobRun | None:
        async with self._sf() as db:
            stmt = (
                select(JobRunRow)
                .where(
                    JobRunRow.idempotency_key == idempotency_key,
                    JobRunRow.status.in_(_ACTIVE),
                )
                .limit(1)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            return _row_to_run(row) if row is not None else None

    async def claim_due(
        self, *, now: datetime, lease_seconds: float, job_names: list[str] | None = None
    ) -> JobRun | None:
        token = uuid.uuid4().hex
        async with self._sf() as db:
            conditions = [
                JobRunRow.status.in_(_CLAIMABLE),
                or_(JobRunRow.available_at.is_(None), JobRunRow.available_at <= now),
            ]
            if job_names is not None:
                conditions.append(JobRunRow.job_name.in_(job_names))
            stmt = (
                select(JobRunRow)
                .where(and_(*conditions))
                .order_by(JobRunRow.available_at.asc().nulls_first(), JobRunRow.scheduled_for.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.status = JobRunStatus.RUNNING
            row.attempt += 1
            row.started_at = now
            row.lease_token = token
            row.lease_until = now + timedelta(seconds=lease_seconds)
            await db.flush()
            return _row_to_run(row)

    async def complete(
        self, run_id: str, *, outcome: RunOutcome, detail: Mapping[str, Any]
    ) -> None:
        status = JobRunStatus.SKIPPED if outcome is RunOutcome.SKIPPED else JobRunStatus.SUCCEEDED
        async with self._sf() as db:
            row = await db.get(JobRunRow, run_id)
            if row is None:
                return
            row.status = status
            row.outcome = outcome
            row.detail = dict(detail)
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_until = None
            await db.flush()

    async def retry(self, run_id: str, *, available_at: datetime, error: str) -> None:
        async with self._sf() as db:
            row = await db.get(JobRunRow, run_id)
            if row is None:
                return
            row.status = JobRunStatus.RETRYING
            row.error = error[:2000]
            row.available_at = available_at
            row.lease_token = None
            row.lease_until = None
            await db.flush()

    async def deadletter(self, run_id: str, *, error: str) -> None:
        async with self._sf() as db:
            row = await db.get(JobRunRow, run_id)
            if row is None:
                return
            row.status = JobRunStatus.DEADLETTER
            row.outcome = RunOutcome.FAILED
            row.error = error[:2000]
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_until = None
            await db.flush()

    async def cancel(self, run_id: str) -> bool:
        async with self._sf() as db:
            row = await db.get(JobRunRow, run_id)
            if row is None or row.status in {
                JobRunStatus.SUCCEEDED,
                JobRunStatus.SKIPPED,
                JobRunStatus.FAILED,
                JobRunStatus.DEADLETTER,
                JobRunStatus.CANCELLED,
            }:
                return False
            row.status = JobRunStatus.CANCELLED
            row.finished_at = datetime.now(UTC)
            row.lease_token = None
            row.lease_until = None
            await db.flush()
            return True

    async def reap_expired(self, *, now: datetime) -> int:
        async with self._sf() as db:
            stmt = (
                select(JobRunRow)
                .where(
                    JobRunRow.status == JobRunStatus.RUNNING,
                    JobRunRow.lease_until.is_not(None),
                    JobRunRow.lease_until <= now,
                )
                .with_for_update(skip_locked=True)
            )
            rows = list((await db.execute(stmt)).scalars().all())
            for row in rows:
                row.status = JobRunStatus.RETRYING
                row.available_at = now
                row.lease_token = None
                row.lease_until = None
            await db.flush()
            return len(rows)

    async def get(self, run_id: str) -> JobRun | None:
        async with self._sf() as db:
            row = await db.get(JobRunRow, run_id)
            return _row_to_run(row) if row is not None else None

    async def list_runs(
        self, *, job_name: str | None = None, status: JobRunStatus | None = None, limit: int = 100
    ) -> list[JobRun]:
        async with self._sf() as db:
            stmt = select(JobRunRow)
            if job_name is not None:
                stmt = stmt.where(JobRunRow.job_name == job_name)
            if status is not None:
                stmt = stmt.where(JobRunRow.status == status)
            stmt = stmt.order_by(JobRunRow.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [_row_to_run(r) for r in rows]

    async def dead_letters(self, *, limit: int = 100) -> list[JobRun]:
        return await self.list_runs(status=JobRunStatus.DEADLETTER, limit=limit)

    async def stats(self) -> StoreStats:
        from sqlalchemy import func

        async with self._sf() as db:
            stmt = select(JobRunRow.status, func.count()).group_by(JobRunRow.status)
            rows = (await db.execute(stmt)).all()
            by_status = {status.value: int(count) for status, count in rows}
            enqueued = sum(by_status.values())
            succeeded = by_status.get(JobRunStatus.SUCCEEDED.value, 0) + by_status.get(
                JobRunStatus.SKIPPED.value, 0
            )
            deadletter = by_status.get(JobRunStatus.DEADLETTER.value, 0)
            failed = deadletter + by_status.get(JobRunStatus.FAILED.value, 0)
            return StoreStats(
                by_status=by_status,
                dead_letters=deadletter,
                enqueued_total=enqueued,
                succeeded_total=succeeded,
                failed_total=failed,
                deadletter_total=deadletter,
            )


__all__ = ["PostgresJobStore"]
