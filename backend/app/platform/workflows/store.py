"""The durable store — where workflow executions and their histories live.

The store is the engine's durability boundary: crash-resume works because the
event history and the schedulable work items (activity tasks, timers, child
starts) survive a process restart in here. Two implementations ship:

* :class:`InMemoryWorkflowStore` (this module) — a fully-functional, zero-infra
  store for unit tests and the deterministic harness. It implements the exact
  same contract as the durable backends, so a test that passes against it is
  proving real engine behaviour, not a mock.
* :class:`~app.platform.workflows.db_store.PostgresWorkflowStore` — the
  Postgres-durable backend over the ``workflow_executions`` / ``workflow_events``
  / ``workflow_tasks`` tables (additive migration ``workflows_0001``).

The contract (a :class:`WorkflowStore` Protocol) covers:

* execution lifecycle — create / load / update status / list;
* the append-only event log — append events atomically with the current
  ``last_event_id`` (optimistic concurrency: a stale appender is rejected, so two
  workers that grabbed the same workflow task can't both commit divergent
  history);
* the **task** queues — ``workflow_tasks`` (a workflow needs a new task because
  new events arrived) and ``activity_tasks`` (an activity needs running), each
  claimable with a lease + visibility timeout for at-least-once delivery;
* durable **timers** — fire-at rows the timer service promotes to ``TIMER_FIRED``.

Everything is keyed so the operations the executor/worker need are O(1)/O(log n).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.platform.workflows.events import HistoryEvent


class ExecutionStatus(enum.StrEnum):
    """Lifecycle of a workflow *execution* (one run id)."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONTINUED_AS_NEW = "continued_as_new"
    TIMED_OUT = "timed_out"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.CONTINUED_AS_NEW,
        ExecutionStatus.TIMED_OUT,
    }
)


@dataclass(slots=True)
class WorkflowExecution:
    """The durable record of one workflow execution (run)."""

    workflow_id: str
    run_id: str
    workflow_type: str
    task_queue: str
    status: ExecutionStatus
    input_args: list[Any]
    input_kwargs: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    last_event_id: int = 0
    attempt: int = 1
    result: Any = None
    error: dict[str, Any] | None = None
    parent_workflow_id: str | None = None
    parent_run_id: str | None = None
    parent_seq: int | None = None
    memo: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_EXECUTION_STATUSES


@dataclass(slots=True)
class WorkflowTask:
    """A signal that a workflow has new events to process (needs a task run)."""

    id: str
    workflow_id: str
    run_id: str
    visible_at: datetime
    lease_token: str | None = None
    lease_until: datetime | None = None
    attempt: int = 0


@dataclass(slots=True)
class ActivityTask:
    """A durable activity execution to run (at-least-once, leased, retried)."""

    id: str
    workflow_id: str
    run_id: str
    seq: int
    activity_type: str
    args: list[Any]
    kwargs: dict[str, Any]
    task_queue: str
    attempt: int
    retry_policy_dict: dict[str, Any] | None
    start_to_close_timeout_s: float | None
    schedule_to_close_timeout_s: float | None
    heartbeat_timeout_s: float | None
    visible_at: datetime
    scheduled_at: datetime
    lease_token: str | None = None
    lease_until: datetime | None = None
    last_heartbeat_at: datetime | None = None


@dataclass(slots=True)
class DurableTimer:
    """A durable timer that fires a ``TIMER_FIRED`` event at ``fire_at``."""

    id: str
    workflow_id: str
    run_id: str
    seq: int
    fire_at: datetime
    cancelled: bool = False


@runtime_checkable
class WorkflowStore(Protocol):
    """The durability contract every backend implements (see module docstring)."""

    # --- executions ---
    async def create_execution(self, execution: WorkflowExecution) -> None: ...
    async def get_execution(
        self, workflow_id: str, run_id: str | None = None
    ) -> WorkflowExecution | None: ...
    async def update_execution(self, execution: WorkflowExecution) -> None: ...
    async def list_executions(
        self, *, status: ExecutionStatus | None = None, limit: int = 100
    ) -> list[WorkflowExecution]: ...

    # --- event log ---
    async def append_events(
        self, workflow_id: str, run_id: str, expected_last_event_id: int, events: list[HistoryEvent]
    ) -> bool: ...
    async def load_history(self, workflow_id: str, run_id: str) -> list[HistoryEvent]: ...

    # --- workflow tasks ---
    async def enqueue_workflow_task(self, task: WorkflowTask) -> None: ...
    async def claim_workflow_task(
        self, *, now: datetime, lease_token: str, lease_s: float
    ) -> WorkflowTask | None: ...
    async def complete_workflow_task(self, task_id: str) -> None: ...

    # --- activity tasks ---
    async def enqueue_activity_task(self, task: ActivityTask) -> None: ...
    async def claim_activity_task(
        self, *, now: datetime, task_queues: Iterable[str], lease_token: str, lease_s: float
    ) -> ActivityTask | None: ...
    async def complete_activity_task(self, task_id: str) -> None: ...
    async def reschedule_activity_task(self, task: ActivityTask) -> None: ...
    async def heartbeat_activity_task(
        self, task_id: str, lease_token: str, now: datetime, lease_s: float
    ) -> bool: ...

    # --- timers ---
    async def add_timer(self, timer: DurableTimer) -> None: ...
    async def cancel_timer(self, workflow_id: str, run_id: str, seq: int) -> None: ...
    async def due_timers(self, now: datetime, limit: int = 100) -> list[DurableTimer]: ...
    async def remove_timer(self, timer_id: str) -> None: ...


@dataclass(slots=True)
class StoreStats:
    """A snapshot of store depths (for metrics / admin / tests)."""

    executions: int
    open_executions: int
    pending_workflow_tasks: int
    pending_activity_tasks: int
    pending_timers: int


__all__ = [
    "TERMINAL_EXECUTION_STATUSES",
    "ActivityTask",
    "DurableTimer",
    "ExecutionStatus",
    "StoreStats",
    "WorkflowExecution",
    "WorkflowStore",
    "WorkflowTask",
]
