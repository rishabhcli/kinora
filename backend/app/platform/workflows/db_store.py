"""A durable :class:`WorkflowStore` over Postgres.

Backs the engine with the five ``workflow_*`` tables (migration
``workflows_0001``), giving crash-durable executions, an auditable event log, and
durable task/timer queues. The two race-prone paths are made safe by the database
itself, exactly mirroring the in-memory store's semantics:

* **Optimistic-concurrency append.** :meth:`append_events` only commits when the
  caller's ``expected_last_event_id`` still matches the stored row, advancing it
  atomically; a racing second appender (two workers that grabbed the same workflow
  task) reads the now-stale value and is rejected — and the ``UNIQUE(workflow_id,
  run_id, event_id)`` constraint is the backstop if two somehow slip through.
* **Exclusive leased claims.** :meth:`claim_workflow_task` /
  :meth:`claim_activity_task` flip the earliest visible, unleased row under
  ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers never claim the same
  task (Postgres' row-lock is the lease's teeth); a crashed worker's lease lapses
  and the row becomes claimable again (at-least-once).

Each method runs in its own short unit of work via an injected committing session
factory (same shape as :func:`app.db.session.get_session`), so the store never
holds a session open across handler execution. Payloads are stored JSON-shaped via
:mod:`app.platform.workflows.serde` so rich types (datetimes, decimals) survive the
round-trip deterministically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workflow import (
    WorkflowActivityTaskRow,
    WorkflowEventRow,
    WorkflowExecutionRow,
    WorkflowTaskRow,
    WorkflowTimerRow,
)
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.ids import new_id
from app.platform.workflows.serde import from_jsonable, to_jsonable
from app.platform.workflows.store import (
    ActivityTask,
    DurableTimer,
    ExecutionStatus,
    StoreStats,
    WorkflowExecution,
    WorkflowTask,
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class PostgresWorkflowStore:
    """Postgres-durable implementation of the :class:`WorkflowStore` contract."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ----- executions --------------------------------------------------------
    async def create_execution(self, execution: WorkflowExecution) -> None:
        async with self._session_factory() as session:
            session.add(
                WorkflowExecutionRow(
                    id=execution.run_id,
                    workflow_id=execution.workflow_id,
                    run_id=execution.run_id,
                    workflow_type=execution.workflow_type,
                    task_queue=execution.task_queue,
                    status=execution.status,
                    input_args=to_jsonable(execution.input_args),
                    input_kwargs=to_jsonable(execution.input_kwargs),
                    last_event_id=execution.last_event_id,
                    attempt=execution.attempt,
                    result=_wrap(execution.result),
                    error=execution.error,
                    parent_workflow_id=execution.parent_workflow_id,
                    parent_run_id=execution.parent_run_id,
                    parent_seq=execution.parent_seq,
                    memo=to_jsonable(execution.memo),
                )
            )

    async def get_execution(
        self, workflow_id: str, run_id: str | None = None
    ) -> WorkflowExecution | None:
        async with self._session_factory() as session:
            stmt = select(WorkflowExecutionRow).where(
                WorkflowExecutionRow.workflow_id == workflow_id
            )
            if run_id is not None:
                stmt = stmt.where(WorkflowExecutionRow.run_id == run_id)
            else:
                stmt = stmt.order_by(WorkflowExecutionRow.created_at.desc())
            row = (await session.execute(stmt.limit(1))).scalar_one_or_none()
            return _row_to_execution(row) if row else None

    async def update_execution(self, execution: WorkflowExecution) -> None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(WorkflowExecutionRow)
                    .where(WorkflowExecutionRow.workflow_id == execution.workflow_id)
                    .where(WorkflowExecutionRow.run_id == execution.run_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.status = execution.status
            row.last_event_id = execution.last_event_id
            row.attempt = execution.attempt
            row.result = _wrap(execution.result)
            row.error = execution.error
            row.memo = to_jsonable(execution.memo)

    async def list_executions(
        self, *, status: ExecutionStatus | None = None, limit: int = 100
    ) -> list[WorkflowExecution]:
        async with self._session_factory() as session:
            stmt = select(WorkflowExecutionRow)
            if status is not None:
                stmt = stmt.where(WorkflowExecutionRow.status == status)
            stmt = stmt.order_by(WorkflowExecutionRow.created_at).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_execution(r) for r in rows]

    # ----- event log ---------------------------------------------------------
    async def append_events(
        self,
        workflow_id: str,
        run_id: str,
        expected_last_event_id: int,
        events: list[HistoryEvent],
    ) -> bool:
        if not events:
            return True
        async with self._session_factory() as session:
            # Atomic compare-and-set on last_event_id (optimistic concurrency).
            result = await session.execute(
                update(WorkflowExecutionRow)
                .where(WorkflowExecutionRow.workflow_id == workflow_id)
                .where(WorkflowExecutionRow.run_id == run_id)
                .where(WorkflowExecutionRow.last_event_id == expected_last_event_id)
                .values(last_event_id=events[-1].event_id, updated_at=events[-1].timestamp)
            )
            if cast("CursorResult[Any]", result).rowcount != 1:
                return False  # stale appender lost the race
            for event in events:
                session.add(
                    WorkflowEventRow(
                        id=new_id("evt"),
                        workflow_id=workflow_id,
                        run_id=run_id,
                        event_id=event.event_id,
                        event_type=event.type.value,
                        timestamp=event.timestamp,
                        attributes=to_jsonable(event.attributes),
                    )
                )
            return True

    async def load_history(self, workflow_id: str, run_id: str) -> list[HistoryEvent]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WorkflowEventRow)
                        .where(WorkflowEventRow.workflow_id == workflow_id)
                        .where(WorkflowEventRow.run_id == run_id)
                        .order_by(WorkflowEventRow.event_id)
                    )
                )
                .scalars()
                .all()
            )
            return [
                HistoryEvent(
                    event_id=r.event_id,
                    type=EventType(r.event_type),
                    timestamp=_aware(r.timestamp) or datetime.now(UTC),
                    attributes=from_jsonable(dict(r.attributes or {})),
                )
                for r in rows
            ]

    # ----- workflow tasks ----------------------------------------------------
    async def enqueue_workflow_task(self, task: WorkflowTask) -> None:
        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(WorkflowTaskRow)
                    .where(WorkflowTaskRow.workflow_id == task.workflow_id)
                    .where(WorkflowTaskRow.run_id == task.run_id)
                    .where(WorkflowTaskRow.lease_token.is_(None))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.visible_at = min(existing.visible_at, task.visible_at)
                return
            session.add(
                WorkflowTaskRow(
                    id=task.id,
                    workflow_id=task.workflow_id,
                    run_id=task.run_id,
                    visible_at=task.visible_at,
                    attempt=task.attempt,
                )
            )

    async def claim_workflow_task(
        self, *, now: datetime, lease_token: str, lease_s: float
    ) -> WorkflowTask | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(WorkflowTaskRow)
                    .where(WorkflowTaskRow.visible_at <= now)
                    .where(
                        (WorkflowTaskRow.lease_until.is_(None))
                        | (WorkflowTaskRow.lease_until <= now)
                    )
                    .order_by(WorkflowTaskRow.visible_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.lease_token = lease_token
            row.lease_until = now + timedelta(seconds=lease_s)
            row.attempt += 1
            return WorkflowTask(
                id=row.id,
                workflow_id=row.workflow_id,
                run_id=row.run_id,
                visible_at=_aware(row.visible_at) or now,
                lease_token=row.lease_token,
                lease_until=_aware(row.lease_until),
                attempt=row.attempt,
            )

    async def complete_workflow_task(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowTaskRow, task_id)
            if row is not None:
                await session.delete(row)

    # ----- activity tasks ----------------------------------------------------
    async def enqueue_activity_task(self, task: ActivityTask) -> None:
        async with self._session_factory() as session:
            session.add(
                WorkflowActivityTaskRow(
                    id=task.id,
                    workflow_id=task.workflow_id,
                    run_id=task.run_id,
                    seq=task.seq,
                    activity_type=task.activity_type,
                    args=to_jsonable(task.args),
                    kwargs=to_jsonable(task.kwargs),
                    task_queue=task.task_queue,
                    attempt=task.attempt,
                    retry_policy=task.retry_policy_dict,
                    start_to_close_timeout_s=task.start_to_close_timeout_s,
                    schedule_to_close_timeout_s=task.schedule_to_close_timeout_s,
                    heartbeat_timeout_s=task.heartbeat_timeout_s,
                    visible_at=task.visible_at,
                    scheduled_at=task.scheduled_at,
                )
            )

    async def claim_activity_task(
        self,
        *,
        now: datetime,
        task_queues: Iterable[str],
        lease_token: str,
        lease_s: float,
    ) -> ActivityTask | None:
        queues = list(task_queues)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(WorkflowActivityTaskRow)
                    .where(WorkflowActivityTaskRow.task_queue.in_(queues))
                    .where(WorkflowActivityTaskRow.visible_at <= now)
                    .where(
                        (WorkflowActivityTaskRow.lease_until.is_(None))
                        | (WorkflowActivityTaskRow.lease_until <= now)
                    )
                    .order_by(WorkflowActivityTaskRow.visible_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.lease_token = lease_token
            row.lease_until = now + timedelta(seconds=lease_s)
            row.last_heartbeat_at = now
            return _row_to_activity_task(row, now)

    async def complete_activity_task(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowActivityTaskRow, task_id)
            if row is not None:
                await session.delete(row)

    async def reschedule_activity_task(self, task: ActivityTask) -> None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowActivityTaskRow, task.id)
            if row is None:
                return
            row.attempt = task.attempt
            row.visible_at = task.visible_at
            row.lease_token = None
            row.lease_until = None
            row.last_heartbeat_at = None

    async def heartbeat_activity_task(
        self, task_id: str, lease_token: str, now: datetime, lease_s: float
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.get(WorkflowActivityTaskRow, task_id)
            if row is None or row.lease_token != lease_token:
                return False
            row.last_heartbeat_at = now
            row.lease_until = now + timedelta(seconds=lease_s)
            return True

    # ----- timers ------------------------------------------------------------
    async def add_timer(self, timer: DurableTimer) -> None:
        async with self._session_factory() as session:
            session.add(
                WorkflowTimerRow(
                    id=timer.id,
                    workflow_id=timer.workflow_id,
                    run_id=timer.run_id,
                    seq=timer.seq,
                    fire_at=timer.fire_at,
                    cancelled=timer.cancelled,
                )
            )

    async def cancel_timer(self, workflow_id: str, run_id: str, seq: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(WorkflowTimerRow)
                .where(WorkflowTimerRow.workflow_id == workflow_id)
                .where(WorkflowTimerRow.run_id == run_id)
                .where(WorkflowTimerRow.seq == seq)
                .values(cancelled=True)
            )

    async def due_timers(self, now: datetime, limit: int = 100) -> list[DurableTimer]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WorkflowTimerRow)
                        .where(WorkflowTimerRow.cancelled.is_(False))
                        .where(WorkflowTimerRow.fire_at <= now)
                        .order_by(WorkflowTimerRow.fire_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [
                DurableTimer(
                    id=r.id,
                    workflow_id=r.workflow_id,
                    run_id=r.run_id,
                    seq=r.seq,
                    fire_at=_aware(r.fire_at) or now,
                    cancelled=r.cancelled,
                )
                for r in rows
            ]

    async def remove_timer(self, timer_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowTimerRow, timer_id)
            if row is not None:
                await session.delete(row)

    # ----- introspection -----------------------------------------------------
    async def stats(self) -> StoreStats:
        from sqlalchemy import func

        async with self._session_factory() as session:
            executions = (
                await session.execute(select(func.count()).select_from(WorkflowExecutionRow))
            ).scalar_one()
            open_executions = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowExecutionRow)
                    .where(WorkflowExecutionRow.status == ExecutionStatus.RUNNING)
                )
            ).scalar_one()
            wtasks = (
                await session.execute(select(func.count()).select_from(WorkflowTaskRow))
            ).scalar_one()
            atasks = (
                await session.execute(select(func.count()).select_from(WorkflowActivityTaskRow))
            ).scalar_one()
            timers = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkflowTimerRow)
                    .where(WorkflowTimerRow.cancelled.is_(False))
                )
            ).scalar_one()
            return StoreStats(
                executions=int(executions),
                open_executions=int(open_executions),
                pending_workflow_tasks=int(wtasks),
                pending_activity_tasks=int(atasks),
                pending_timers=int(timers),
            )


_RESULT_KEY = "__wf_result__"


def _wrap(value: Any) -> dict[str, Any] | None:
    """Wrap an arbitrary (possibly scalar) workflow result for a JSON column."""
    if value is None:
        return None
    return {_RESULT_KEY: to_jsonable(value)}


def _unwrap(value: dict[str, Any] | None) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and _RESULT_KEY in value:
        return from_jsonable(value[_RESULT_KEY])
    return from_jsonable(value)


def _row_to_execution(row: WorkflowExecutionRow) -> WorkflowExecution:
    return WorkflowExecution(
        workflow_id=row.workflow_id,
        run_id=row.run_id,
        workflow_type=row.workflow_type,
        task_queue=row.task_queue,
        status=row.status,
        input_args=list(from_jsonable(row.input_args or [])),
        input_kwargs=dict(from_jsonable(row.input_kwargs or {})),
        created_at=_aware(row.created_at) or datetime.now(UTC),
        updated_at=_aware(row.updated_at) or datetime.now(UTC),
        last_event_id=row.last_event_id,
        attempt=row.attempt,
        result=_unwrap(row.result),
        error=dict(row.error) if row.error else None,
        parent_workflow_id=row.parent_workflow_id,
        parent_run_id=row.parent_run_id,
        parent_seq=row.parent_seq,
        memo=dict(from_jsonable(row.memo or {})),
    )


def _row_to_activity_task(row: WorkflowActivityTaskRow, now: datetime) -> ActivityTask:
    return ActivityTask(
        id=row.id,
        workflow_id=row.workflow_id,
        run_id=row.run_id,
        seq=row.seq,
        activity_type=row.activity_type,
        args=list(from_jsonable(row.args or [])),
        kwargs=dict(from_jsonable(row.kwargs or {})),
        task_queue=row.task_queue,
        attempt=row.attempt,
        retry_policy_dict=dict(row.retry_policy) if row.retry_policy else None,
        start_to_close_timeout_s=row.start_to_close_timeout_s,
        schedule_to_close_timeout_s=row.schedule_to_close_timeout_s,
        heartbeat_timeout_s=row.heartbeat_timeout_s,
        visible_at=_aware(row.visible_at) or now,
        scheduled_at=_aware(row.scheduled_at) or now,
        lease_token=row.lease_token,
        lease_until=_aware(row.lease_until),
        last_heartbeat_at=_aware(row.last_heartbeat_at),
    )


__all__ = ["PostgresWorkflowStore"]
