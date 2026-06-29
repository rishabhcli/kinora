"""In-memory :class:`WorkflowStore` — the zero-infra durable-store implementation.

Implements the full :class:`~app.platform.workflows.store.WorkflowStore` contract
in process memory. It is *not* a mock: the harness and worker drive it exactly as
they would a Postgres backend, so a green test here is real engine behaviour. It
models the durability semantics that matter for correctness:

* **optimistic-concurrency append** — :meth:`append_events` only commits if the
  caller's ``expected_last_event_id`` matches the stored one (rejecting a second
  worker that raced on the same task);
* **leased claims with visibility timeout** — workflow/activity tasks become
  claimable only after ``visible_at`` and, once claimed, are invisible until the
  lease expires; a crashed worker's lease lapses and the task is re-delivered
  (at-least-once);
* **deep-copied payloads on the boundary** — args/results are copied in and out so
  callers can't mutate stored state by reference (mirrors a real wire boundary).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable
from datetime import datetime, timedelta

from app.platform.workflows.events import HistoryEvent
from app.platform.workflows.store import (
    ActivityTask,
    DurableTimer,
    ExecutionStatus,
    StoreStats,
    WorkflowExecution,
    WorkflowTask,
)


class InMemoryWorkflowStore:
    """A complete, in-process implementation of the store contract."""

    def __init__(self) -> None:
        # keyed by workflow_id -> {run_id -> execution}; latest run tracked too.
        self._executions: dict[tuple[str, str], WorkflowExecution] = {}
        self._latest_run: dict[str, str] = {}
        self._history: dict[tuple[str, str], list[HistoryEvent]] = {}
        self._workflow_tasks: dict[str, WorkflowTask] = {}
        self._activity_tasks: dict[str, ActivityTask] = {}
        self._timers: dict[str, DurableTimer] = {}
        self._lock = asyncio.Lock()

    # ----- executions --------------------------------------------------------
    async def create_execution(self, execution: WorkflowExecution) -> None:
        async with self._lock:
            key = (execution.workflow_id, execution.run_id)
            self._executions[key] = _clone_execution(execution)
            self._latest_run[execution.workflow_id] = execution.run_id
            self._history.setdefault(key, [])

    async def get_execution(
        self, workflow_id: str, run_id: str | None = None
    ) -> WorkflowExecution | None:
        async with self._lock:
            rid = run_id or self._latest_run.get(workflow_id)
            if rid is None:
                return None
            execution = self._executions.get((workflow_id, rid))
            return _clone_execution(execution) if execution else None

    async def update_execution(self, execution: WorkflowExecution) -> None:
        async with self._lock:
            self._executions[(execution.workflow_id, execution.run_id)] = _clone_execution(
                execution
            )

    async def list_executions(
        self, *, status: ExecutionStatus | None = None, limit: int = 100
    ) -> list[WorkflowExecution]:
        async with self._lock:
            out = [
                _clone_execution(e)
                for e in self._executions.values()
                if status is None or e.status == status
            ]
        out.sort(key=lambda e: e.created_at)
        return out[:limit]

    # ----- event log ---------------------------------------------------------
    async def append_events(
        self,
        workflow_id: str,
        run_id: str,
        expected_last_event_id: int,
        events: list[HistoryEvent],
    ) -> bool:
        async with self._lock:
            key = (workflow_id, run_id)
            execution = self._executions.get(key)
            if execution is None:
                return False
            if execution.last_event_id != expected_last_event_id:
                return False  # optimistic-concurrency conflict
            log = self._history.setdefault(key, [])
            for event in events:
                log.append(_clone_event(event))
            if events:
                execution.last_event_id = events[-1].event_id
                execution.updated_at = events[-1].timestamp
            return True

    async def load_history(self, workflow_id: str, run_id: str) -> list[HistoryEvent]:
        async with self._lock:
            return [_clone_event(e) for e in self._history.get((workflow_id, run_id), [])]

    # ----- workflow tasks ----------------------------------------------------
    async def enqueue_workflow_task(self, task: WorkflowTask) -> None:
        async with self._lock:
            # Collapse duplicates: at most one outstanding task per run.
            for existing in self._workflow_tasks.values():
                if (
                    existing.workflow_id == task.workflow_id
                    and existing.run_id == task.run_id
                    and existing.lease_token is None
                ):
                    existing.visible_at = min(existing.visible_at, task.visible_at)
                    return
            self._workflow_tasks[task.id] = task

    async def claim_workflow_task(
        self, *, now: datetime, lease_token: str, lease_s: float
    ) -> WorkflowTask | None:
        async with self._lock:
            for task in sorted(self._workflow_tasks.values(), key=lambda t: t.visible_at):
                if _claimable(task.lease_until, task.visible_at, now):
                    task.lease_token = lease_token
                    task.lease_until = now + timedelta(seconds=lease_s)
                    task.attempt += 1
                    return _clone_workflow_task(task)
            return None

    async def complete_workflow_task(self, task_id: str) -> None:
        async with self._lock:
            self._workflow_tasks.pop(task_id, None)

    # ----- activity tasks ----------------------------------------------------
    async def enqueue_activity_task(self, task: ActivityTask) -> None:
        async with self._lock:
            self._activity_tasks[task.id] = _clone_activity_task(task)

    async def claim_activity_task(
        self,
        *,
        now: datetime,
        task_queues: Iterable[str],
        lease_token: str,
        lease_s: float,
    ) -> ActivityTask | None:
        queues = set(task_queues)
        async with self._lock:
            for task in sorted(self._activity_tasks.values(), key=lambda t: t.visible_at):
                if task.task_queue not in queues:
                    continue
                if _claimable(task.lease_until, task.visible_at, now):
                    task.lease_token = lease_token
                    task.lease_until = now + timedelta(seconds=lease_s)
                    task.last_heartbeat_at = now
                    return _clone_activity_task(task)
            return None

    async def complete_activity_task(self, task_id: str) -> None:
        async with self._lock:
            self._activity_tasks.pop(task_id, None)

    async def reschedule_activity_task(self, task: ActivityTask) -> None:
        async with self._lock:
            stored = self._activity_tasks.get(task.id)
            if stored is None:
                return
            stored.attempt = task.attempt
            stored.visible_at = task.visible_at
            stored.lease_token = None
            stored.lease_until = None
            stored.last_heartbeat_at = None

    async def heartbeat_activity_task(
        self, task_id: str, lease_token: str, now: datetime, lease_s: float
    ) -> bool:
        async with self._lock:
            task = self._activity_tasks.get(task_id)
            if task is None or task.lease_token != lease_token:
                return False
            task.last_heartbeat_at = now
            task.lease_until = now + timedelta(seconds=lease_s)
            return True

    # ----- timers ------------------------------------------------------------
    async def add_timer(self, timer: DurableTimer) -> None:
        async with self._lock:
            self._timers[timer.id] = timer

    async def cancel_timer(self, workflow_id: str, run_id: str, seq: int) -> None:
        async with self._lock:
            for timer in self._timers.values():
                if timer.workflow_id == workflow_id and timer.run_id == run_id and timer.seq == seq:
                    timer.cancelled = True

    async def due_timers(self, now: datetime, limit: int = 100) -> list[DurableTimer]:
        async with self._lock:
            due = [t for t in self._timers.values() if not t.cancelled and t.fire_at <= now]
        due.sort(key=lambda t: t.fire_at)
        return due[:limit]

    async def remove_timer(self, timer_id: str) -> None:
        async with self._lock:
            self._timers.pop(timer_id, None)

    # ----- introspection -----------------------------------------------------
    async def stats(self) -> StoreStats:
        async with self._lock:
            return StoreStats(
                executions=len(self._executions),
                open_executions=sum(1 for e in self._executions.values() if not e.is_terminal),
                pending_workflow_tasks=len(self._workflow_tasks),
                pending_activity_tasks=len(self._activity_tasks),
                pending_timers=sum(1 for t in self._timers.values() if not t.cancelled),
            )


def _claimable(lease_until: datetime | None, visible_at: datetime, now: datetime) -> bool:
    if visible_at > now:
        return False
    return lease_until is None or lease_until <= now


def _clone_execution(execution: WorkflowExecution | None) -> WorkflowExecution:
    assert execution is not None
    return copy.deepcopy(execution)


def _clone_event(event: HistoryEvent) -> HistoryEvent:
    return HistoryEvent(
        event.event_id, event.type, event.timestamp, copy.deepcopy(event.attributes)
    )


def _clone_workflow_task(task: WorkflowTask) -> WorkflowTask:
    return copy.deepcopy(task)


def _clone_activity_task(task: ActivityTask) -> ActivityTask:
    return copy.deepcopy(task)


__all__ = ["InMemoryWorkflowStore"]
