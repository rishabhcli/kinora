"""``WorkflowClient`` — start, signal, query, cancel, and await executions.

The client is the *outside world's* handle on the engine (the API layer, another
service, a test). It never runs workflow code itself; it manipulates durable
state and lets the worker runtime advance things:

* :meth:`start_workflow` — create an execution (idempotent on ``workflow_id``: a
  duplicate start returns the existing run rather than double-spending) and seed
  its first ``WORKFLOW_STARTED`` event + a workflow task.
* :meth:`signal_workflow` — append a ``SIGNAL_RECEIVED`` event and wake the
  workflow (the durable, asynchronous channel into a running execution; this is
  how the episode workflow gets "director approved scene 3").
* :meth:`signal_with_start` — signal an execution, starting it first if absent
  (the common "ensure-running then notify" pattern).
* :meth:`query_workflow` — synchronously evaluate a registered query handler
  against the *current* state by replaying history read-only (no events appended);
  this is the read-side that never mutates the run.
* :meth:`cancel_workflow` — request cancellation (a ``WORKFLOW_CANCEL_REQUESTED``
  event the workflow observes via ``ctx.is_cancelled``).
* :meth:`get_result` / :meth:`describe` — read terminal result / status.

All mutations go through the store's optimistic-concurrency append, so the client
is safe to call concurrently with the workers.
"""

from __future__ import annotations

from typing import Any

from app.jobs.clock import Clock, SystemClock
from app.platform.workflows.context import WorkflowInfo
from app.platform.workflows.errors import (
    WorkflowAlreadyExistsError,
    WorkflowNotFoundError,
)
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.ids import new_id
from app.platform.workflows.registry import WorkflowRegistry
from app.platform.workflows.replay import build_replay_state
from app.platform.workflows.store import (
    ExecutionStatus,
    WorkflowExecution,
    WorkflowStore,
    WorkflowTask,
)


class WorkflowHandle:
    """A lightweight reference to a started execution (id + run id)."""

    __slots__ = ("workflow_id", "run_id")

    def __init__(self, workflow_id: str, run_id: str) -> None:
        self.workflow_id = workflow_id
        self.run_id = run_id


class WorkflowClient:
    """Start/signal/query/cancel handle on the durable engine."""

    def __init__(
        self,
        store: WorkflowStore,
        workflows: WorkflowRegistry,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._workflows = workflows
        self._clock = clock or SystemClock()

    async def start_workflow(
        self,
        workflow_type: str,
        *args: Any,
        workflow_id: str,
        task_queue: str | None = None,
        **kwargs: Any,
    ) -> WorkflowHandle:
        """Start an execution; idempotent on a still-open ``workflow_id``."""
        definition = self._workflows.get(workflow_type)
        existing = await self._store.get_execution(workflow_id)
        if existing is not None and not existing.is_terminal:
            raise WorkflowAlreadyExistsError(workflow_id)
        now = self._clock.now()
        run_id = new_id("run")
        execution = WorkflowExecution(
            workflow_id=workflow_id,
            run_id=run_id,
            workflow_type=workflow_type,
            task_queue=task_queue or definition.default_task_queue,
            status=ExecutionStatus.RUNNING,
            input_args=list(args),
            input_kwargs=dict(kwargs),
            created_at=now,
            updated_at=now,
        )
        await self._store.create_execution(execution)
        start_event = HistoryEvent(
            1, EventType.WORKFLOW_STARTED, now, {"args": list(args), "kwargs": dict(kwargs)}
        )
        await self._store.append_events(workflow_id, run_id, 0, [start_event])
        await self._store.enqueue_workflow_task(
            WorkflowTask(id=new_id("wft"), workflow_id=workflow_id, run_id=run_id, visible_at=now)
        )
        return WorkflowHandle(workflow_id, run_id)

    async def signal_workflow(self, workflow_id: str, name: str, payload: Any = None) -> None:
        """Deliver a signal to a running execution and wake it."""
        execution = await self._store.get_execution(workflow_id)
        if execution is None:
            raise WorkflowNotFoundError(workflow_id)
        if execution.is_terminal:
            return  # signals to a finished workflow are dropped (Temporal-like)
        await self._append_external(
            execution, EventType.SIGNAL_RECEIVED, {"name": name, "payload": payload}
        )

    async def signal_with_start(
        self,
        workflow_type: str,
        name: str,
        payload: Any = None,
        *,
        workflow_id: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        task_queue: str | None = None,
    ) -> WorkflowHandle:
        """Signal ``workflow_id``, starting it first if it isn't running."""
        execution = await self._store.get_execution(workflow_id)
        if execution is None or execution.is_terminal:
            handle = await self.start_workflow(
                workflow_type,
                *(args or []),
                workflow_id=workflow_id,
                task_queue=task_queue,
                **(kwargs or {}),
            )
        else:
            handle = WorkflowHandle(execution.workflow_id, execution.run_id)
        await self.signal_workflow(workflow_id, name, payload)
        return handle

    async def cancel_workflow(self, workflow_id: str) -> None:
        """Request cancellation of a running execution."""
        execution = await self._store.get_execution(workflow_id)
        if execution is None:
            raise WorkflowNotFoundError(workflow_id)
        if execution.is_terminal:
            return
        await self._append_external(execution, EventType.WORKFLOW_CANCEL_REQUESTED, {})

    async def query_workflow(self, workflow_id: str, name: str, *args: Any, **kwargs: Any) -> Any:
        """Evaluate a registered query handler over current state (read-only).

        Replays the workflow body to the point it parks (which registers its query
        handlers and rebuilds in-memory state), then invokes the handler. No
        events are appended; the workflow is untouched.
        """
        execution = await self._store.get_execution(workflow_id)
        if execution is None:
            raise WorkflowNotFoundError(workflow_id)
        history = await self._store.load_history(workflow_id, execution.run_id)
        definition = self._workflows.get(execution.workflow_type)
        state = build_replay_state(history)
        from app.platform.workflows.context import WorkflowContext

        info = WorkflowInfo(
            workflow_id=execution.workflow_id,
            run_id=execution.run_id,
            workflow_type=execution.workflow_type,
            task_queue=execution.task_queue,
            attempt=execution.attempt,
        )
        ctx = WorkflowContext(state, info)
        from app.platform.workflows.replay import run_workflow_coroutine

        coro = definition.fn(ctx, *execution.input_args, **execution.input_kwargs)
        run_workflow_coroutine(coro)  # advances to a park; registers query handlers
        return ctx.run_query(name, *args, **kwargs)

    async def get_result(self, workflow_id: str) -> Any:
        """Return the terminal result, or raise if the run isn't finished/ok."""
        execution = await self._store.get_execution(workflow_id)
        if execution is None:
            raise WorkflowNotFoundError(workflow_id)
        if execution.status == ExecutionStatus.COMPLETED:
            return execution.result
        if execution.status == ExecutionStatus.FAILED:
            from app.platform.workflows.errors import ApplicationError

            err = execution.error or {}
            raise ApplicationError(err.get("message", "workflow failed"), type=err.get("type"))
        raise WorkflowNotFoundError(f"{workflow_id} (status={execution.status})")

    async def describe(self, workflow_id: str) -> WorkflowExecution | None:
        """Return the full execution record (status, result, parent links, …)."""
        return await self._store.get_execution(workflow_id)

    async def _append_external(
        self, execution: WorkflowExecution, event_type: EventType, attrs: dict[str, Any]
    ) -> None:
        now = self._clock.now()
        # Retry the append against a fresh last_event_id if we lost a race.
        for _ in range(8):
            current = await self._store.get_execution(execution.workflow_id, execution.run_id)
            if current is None or current.is_terminal:
                return
            event = HistoryEvent(current.last_event_id + 1, event_type, now, attrs)
            if await self._store.append_events(
                execution.workflow_id, execution.run_id, current.last_event_id, [event]
            ):
                await self._store.enqueue_workflow_task(
                    WorkflowTask(
                        id=new_id("wft"),
                        workflow_id=execution.workflow_id,
                        run_id=execution.run_id,
                        visible_at=now,
                    )
                )
                return
        raise RuntimeError("failed to append external event after retries (persistent contention)")


__all__ = ["WorkflowClient", "WorkflowHandle"]
