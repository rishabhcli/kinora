"""The worker runtime — the loops that actually move executions forward.

The executor is pure (history in, events + dispatches out); the **worker** is
where those dispatches become durable state and real work. Three cooperating
roles, each a poll loop over the store, driven by an injected
:class:`~app.jobs.clock.Clock` (so the deterministic harness can step them in
virtual time):

* :class:`WorkflowTaskProcessor` — claims a workflow task, loads history, runs
  :func:`execute_workflow_task`, then **atomically** appends the new events
  (optimistic-concurrency guarded), enqueues the resulting activity tasks /
  arms the timers / starts the child workflows, and updates execution status. A
  workflow task that produces no progress (the workflow merely parked) is just
  completed — the next task is triggered when an activity/timer completion
  enqueues one.
* :class:`ActivityTaskProcessor` — claims an activity task, runs the registered
  activity function under start-to-close + heartbeat timeouts, and on
  success/failure records the completion event and **enqueues a workflow task** so
  the workflow advances. Failures consult the :class:`RetryPolicy`: retry
  (reschedule with backoff) or give up (record ``ACTIVITY_FAILED`` →
  surfaced as a catchable :class:`ActivityFailure` inside the workflow).
* :class:`TimerService` — promotes due durable timers to ``TIMER_FIRED`` events
  and enqueues a workflow task.

At-least-once is the contract throughout: a crash after running an activity but
before recording its completion re-delivers the activity (the lease lapses), and
the activity *should* be idempotent — exactly the render-queue discipline
(idempotency key = ``shot_hash``) generalised. The workflow side is *exactly*-once
in its effect on history because of the optimistic-concurrency append + the
deterministic seq mapping: a re-run that re-schedules an already-scheduled
activity is matched, not duplicated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.jobs.clock import Clock, SystemClock
from app.platform.workflows.context import WorkflowInfo
from app.platform.workflows.errors import ActivityCancelled, ApplicationError
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.executor import (
    ChildWorkflowDispatch,
    WorkflowTaskOutcome,
    execute_workflow_task,
)
from app.platform.workflows.heartbeat import Heartbeater
from app.platform.workflows.ids import new_id
from app.platform.workflows.metrics import (
    record_activity_outcome,
    record_execution_terminal,
    record_timers_fired,
)
from app.platform.workflows.registry import (
    ActivityRegistry,
    WorkflowRegistry,
)
from app.platform.workflows.retry import RetryPolicy
from app.platform.workflows.store import (
    ActivityTask,
    DurableTimer,
    ExecutionStatus,
    WorkflowExecution,
    WorkflowStore,
    WorkflowTask,
)


@dataclass(slots=True)
class WorkerConfig:
    """Tunables for the worker loops (all in seconds)."""

    workflow_task_lease_s: float = 30.0
    activity_task_lease_s: float = 30.0
    poll_interval_s: float = 0.05
    default_start_to_close_s: float = 300.0


class WorkflowTaskProcessor:
    """Drains workflow tasks: replay → persist events → dispatch → status."""

    def __init__(
        self,
        store: WorkflowStore,
        workflows: WorkflowRegistry,
        *,
        clock: Clock | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._store = store
        self._workflows = workflows
        self._clock = clock or SystemClock()
        self._config = config or WorkerConfig()

    async def process_one(self) -> bool:
        """Claim and process a single workflow task. Returns True if one ran."""
        now = self._clock.now()
        token = new_id("wft")
        task = await self._store.claim_workflow_task(
            now=now, lease_token=token, lease_s=self._config.workflow_task_lease_s
        )
        if task is None:
            return False
        await self._run_task(task, now)
        await self._store.complete_workflow_task(task.id)
        return True

    async def _run_task(self, task: WorkflowTask, now: datetime) -> None:
        execution = await self._store.get_execution(task.workflow_id, task.run_id)
        if execution is None or execution.is_terminal:
            return
        history = await self._store.load_history(task.workflow_id, task.run_id)
        definition = self._workflows.get(execution.workflow_type)
        info = WorkflowInfo(
            workflow_id=execution.workflow_id,
            run_id=execution.run_id,
            workflow_type=execution.workflow_type,
            task_queue=execution.task_queue,
            attempt=execution.attempt,
        )
        outcome = execute_workflow_task(
            definition=definition,
            history=history,
            info=info,
            workflow_args=execution.input_args,
            workflow_kwargs=execution.input_kwargs,
            now=now,
        )
        if not outcome.new_events:
            return  # parked; nothing to persist
        committed = await self._store.append_events(
            task.workflow_id, task.run_id, execution.last_event_id, outcome.new_events
        )
        if not committed:
            return  # lost the optimistic-concurrency race; another worker won
        await self._dispatch(execution, outcome, now)
        await self._finalise(execution, outcome, now)

    async def _dispatch(
        self, execution: WorkflowExecution, outcome: WorkflowTaskOutcome, now: datetime
    ) -> None:
        for act in outcome.activities:
            await self._store.enqueue_activity_task(
                ActivityTask(
                    id=new_id("act"),
                    workflow_id=execution.workflow_id,
                    run_id=execution.run_id,
                    seq=act.seq,
                    activity_type=act.activity_type,
                    args=act.args,
                    kwargs=act.kwargs,
                    task_queue=act.task_queue or execution.task_queue,
                    attempt=0,
                    retry_policy_dict=act.retry_policy_dict,
                    start_to_close_timeout_s=act.start_to_close_timeout_s,
                    schedule_to_close_timeout_s=act.schedule_to_close_timeout_s,
                    heartbeat_timeout_s=act.heartbeat_timeout_s,
                    visible_at=now,
                    scheduled_at=now,
                )
            )
        for timer in outcome.timers:
            await self._store.add_timer(
                DurableTimer(
                    id=new_id("tmr"),
                    workflow_id=execution.workflow_id,
                    run_id=execution.run_id,
                    seq=timer.seq,
                    fire_at=timer.fire_at,
                )
            )
        for cancel in outcome.timer_cancels:
            await self._store.cancel_timer(
                execution.workflow_id, execution.run_id, cancel.timer_seq
            )
        for child in outcome.children:
            await self._start_child(execution, child, now)

    async def _start_child(
        self, parent: WorkflowExecution, child: ChildWorkflowDispatch, now: datetime
    ) -> None:
        existing = await self._store.get_execution(child.child_workflow_id)
        if existing is not None:
            return  # idempotent: the child already exists (replay/re-delivery)
        run_id = new_id("run")
        child_exec = WorkflowExecution(
            workflow_id=child.child_workflow_id,
            run_id=run_id,
            workflow_type=child.workflow_type,
            task_queue=child.task_queue or parent.task_queue,
            status=ExecutionStatus.RUNNING,
            input_args=child.args,
            input_kwargs=child.kwargs,
            created_at=now,
            updated_at=now,
            parent_workflow_id=parent.workflow_id,
            parent_run_id=parent.run_id,
            parent_seq=child.seq,
        )
        await self._store.create_execution(child_exec)
        start_event = HistoryEvent(
            1,
            EventType.WORKFLOW_STARTED,
            now,
            {"args": child.args, "kwargs": child.kwargs, "workflow_type": child.workflow_type},
        )
        await self._store.append_events(child.child_workflow_id, run_id, 0, [start_event])
        await self._store.enqueue_workflow_task(
            WorkflowTask(
                id=new_id("wft"), workflow_id=child.child_workflow_id, run_id=run_id, visible_at=now
            )
        )

    async def _finalise(
        self, execution: WorkflowExecution, outcome: WorkflowTaskOutcome, now: datetime
    ) -> None:
        reloaded = await self._store.get_execution(execution.workflow_id, execution.run_id)
        if reloaded is None:
            return
        if outcome.completed:
            reloaded.status = ExecutionStatus.COMPLETED
            reloaded.result = outcome.result
            await self._store.update_execution(reloaded)
            record_execution_terminal(reloaded.workflow_type, "completed")
            await self._notify_parent(reloaded, now, failed=False)
        elif outcome.failed and outcome.error is not None:
            reloaded.status = ExecutionStatus.FAILED
            reloaded.error = {
                "message": outcome.error.message,
                "type": outcome.error.type,
                "details": outcome.error.details,
            }
            await self._store.update_execution(reloaded)
            record_execution_terminal(reloaded.workflow_type, "failed")
            await self._notify_parent(reloaded, now, failed=True)
        elif outcome.continued_as_new:
            await self._continue_as_new(reloaded, outcome, now)

    async def _continue_as_new(
        self, execution: WorkflowExecution, outcome: WorkflowTaskOutcome, now: datetime
    ) -> None:
        execution.status = ExecutionStatus.CONTINUED_AS_NEW
        await self._store.update_execution(execution)
        new_run = new_id("run")
        fresh = WorkflowExecution(
            workflow_id=execution.workflow_id,
            run_id=new_run,
            workflow_type=execution.workflow_type,
            task_queue=execution.task_queue,
            status=ExecutionStatus.RUNNING,
            input_args=outcome.continue_args,
            input_kwargs=outcome.continue_kwargs,
            created_at=now,
            updated_at=now,
            attempt=1,
            parent_workflow_id=execution.parent_workflow_id,
            parent_run_id=execution.parent_run_id,
            parent_seq=execution.parent_seq,
        )
        await self._store.create_execution(fresh)
        start_event = HistoryEvent(
            1,
            EventType.WORKFLOW_STARTED,
            now,
            {"args": outcome.continue_args, "kwargs": outcome.continue_kwargs},
        )
        await self._store.append_events(execution.workflow_id, new_run, 0, [start_event])
        await self._store.enqueue_workflow_task(
            WorkflowTask(
                id=new_id("wft"), workflow_id=execution.workflow_id, run_id=new_run, visible_at=now
            )
        )

    async def _notify_parent(
        self, child: WorkflowExecution, now: datetime, *, failed: bool
    ) -> None:
        if (
            child.parent_workflow_id is None
            or child.parent_run_id is None
            or child.parent_seq is None
        ):
            return
        parent = await self._store.get_execution(child.parent_workflow_id, child.parent_run_id)
        if parent is None or parent.is_terminal:
            return
        if failed:
            event = HistoryEvent(
                parent.last_event_id + 1,
                EventType.CHILD_WORKFLOW_FAILED,
                now,
                {
                    "seq": child.parent_seq,
                    "workflow_type": child.workflow_type,
                    "error": child.error or {},
                },
            )
        else:
            event = HistoryEvent(
                parent.last_event_id + 1,
                EventType.CHILD_WORKFLOW_COMPLETED,
                now,
                {
                    "seq": child.parent_seq,
                    "workflow_type": child.workflow_type,
                    "result": child.result,
                },
            )
        if await self._store.append_events(
            child.parent_workflow_id, child.parent_run_id, parent.last_event_id, [event]
        ):
            await self._store.enqueue_workflow_task(
                WorkflowTask(
                    id=new_id("wft"),
                    workflow_id=child.parent_workflow_id,
                    run_id=child.parent_run_id,
                    visible_at=now,
                )
            )


class ActivityTaskProcessor:
    """Runs activity tasks with timeouts + heartbeat, records completions."""

    def __init__(
        self,
        store: WorkflowStore,
        activities: ActivityRegistry,
        *,
        task_queues: Iterable[str] = ("default",),
        clock: Clock | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._store = store
        self._activities = activities
        self._task_queues = list(task_queues)
        self._clock = clock or SystemClock()
        self._config = config or WorkerConfig()

    async def process_one(self) -> bool:
        """Claim and run a single activity task. Returns True if one ran."""
        now = self._clock.now()
        token = new_id("alt")
        task = await self._store.claim_activity_task(
            now=now,
            task_queues=self._task_queues,
            lease_token=token,
            lease_s=self._config.activity_task_lease_s,
        )
        if task is None:
            return False
        await self._run(task, token)
        return True

    async def _run(self, task: ActivityTask, lease_token: str) -> None:
        definition = self._activities.get(task.activity_type)
        # Fill any options the workflow command left unset from the registered
        # definition's defaults (the @activity decorator), so callers can rely on
        # ``execute_activity("name")`` honouring the activity's declared
        # retry/timeout policy without restating it at every call site.
        if task.start_to_close_timeout_s is None:
            task.start_to_close_timeout_s = definition.start_to_close_timeout_s
        if task.heartbeat_timeout_s is None:
            task.heartbeat_timeout_s = definition.heartbeat_timeout_s
        if task.retry_policy_dict is None:
            task.retry_policy_dict = definition.retry_policy.to_dict()
        attempt = task.attempt + 1
        heartbeater = Heartbeater(
            store=self._store,
            task_id=task.id,
            lease_token=lease_token,
            clock=self._clock,
            lease_s=self._config.activity_task_lease_s,
        )
        timeout_s = task.start_to_close_timeout_s or self._config.default_start_to_close_s
        try:
            result = await self._invoke(definition.fn, task, heartbeater, timeout_s)
        except ActivityCancelled:
            await self._record_cancelled(task)
            return
        except TimeoutError:
            await self._record_timeout(task, "start_to_close")
            return
        except ApplicationError as exc:
            await self._handle_failure(task, attempt, exc)
            return
        except Exception as exc:  # noqa: BLE001 - any activity error → app error
            await self._handle_failure(
                task, attempt, ApplicationError(str(exc), type=type(exc).__name__)
            )
            return
        await self._record_success(task, result)

    async def _invoke(
        self,
        fn: Callable[..., Any],
        task: ActivityTask,
        heartbeater: Heartbeater,
        timeout_s: float,
    ) -> Any:
        async def _call() -> Any:
            from app.platform.workflows.activity import ActivityContext

            actx = ActivityContext(
                workflow_id=task.workflow_id,
                run_id=task.run_id,
                activity_type=task.activity_type,
                attempt=task.attempt + 1,
                heartbeater=heartbeater,
            )
            if definition_is_async(fn):
                return await fn(actx, *task.args, **task.kwargs)
            return fn(actx, *task.args, **task.kwargs)

        return await asyncio.wait_for(_call(), timeout=timeout_s)

    async def _record_success(self, task: ActivityTask, result: Any) -> None:
        await self._append_completion(
            task, EventType.ACTIVITY_COMPLETED, {"seq": task.seq, "result": result}
        )
        record_activity_outcome(task.activity_type, "succeeded")
        await self._store.complete_activity_task(task.id)

    async def _record_cancelled(self, task: ActivityTask) -> None:
        await self._append_completion(
            task,
            EventType.ACTIVITY_CANCELLED,
            {"seq": task.seq, "activity_type": task.activity_type},
        )
        record_activity_outcome(task.activity_type, "cancelled")
        await self._store.complete_activity_task(task.id)

    async def _record_timeout(self, task: ActivityTask, kind: str) -> None:
        await self._append_completion(
            task,
            EventType.ACTIVITY_TIMED_OUT,
            {"seq": task.seq, "activity_type": task.activity_type, "timeout_kind": kind},
        )
        record_activity_outcome(task.activity_type, "timed_out")
        await self._store.complete_activity_task(task.id)

    async def _handle_failure(
        self, task: ActivityTask, attempt: int, error: ApplicationError
    ) -> None:
        policy = (
            RetryPolicy.from_dict(task.retry_policy_dict)
            if task.retry_policy_dict
            else RetryPolicy()
        )
        retry = policy.should_retry(
            attempt=attempt, non_retryable=error.non_retryable, error_type=error.type
        )
        if retry:
            delay = policy.delay_for(next_attempt=attempt + 1)
            task.attempt = attempt
            task.visible_at = self._clock.now() + timedelta(seconds=delay)
            await self._store.reschedule_activity_task(task)
            record_activity_outcome(task.activity_type, "retried")
            return
        record_activity_outcome(task.activity_type, "failed")
        await self._append_completion(
            task,
            EventType.ACTIVITY_FAILED,
            {
                "seq": task.seq,
                "activity_type": task.activity_type,
                "error": {
                    "message": error.message,
                    "type": error.type,
                    "non_retryable": error.non_retryable,
                    "details": error.details,
                },
            },
        )
        await self._store.complete_activity_task(task.id)

    async def _append_completion(
        self, task: ActivityTask, event_type: EventType, attrs: dict[str, Any]
    ) -> None:
        now = self._clock.now()
        parent = await self._store.get_execution(task.workflow_id, task.run_id)
        if parent is None or parent.is_terminal:
            return
        event = HistoryEvent(parent.last_event_id + 1, event_type, now, attrs)
        if await self._store.append_events(
            task.workflow_id, task.run_id, parent.last_event_id, [event]
        ):
            await self._store.enqueue_workflow_task(
                WorkflowTask(
                    id=new_id("wft"),
                    workflow_id=task.workflow_id,
                    run_id=task.run_id,
                    visible_at=now,
                )
            )


class TimerService:
    """Promotes due durable timers to ``TIMER_FIRED`` and wakes the workflow."""

    def __init__(self, store: WorkflowStore, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or SystemClock()

    async def fire_due(self) -> int:
        """Fire all currently-due timers. Returns how many fired."""
        now = self._clock.now()
        due = await self._store.due_timers(now)
        fired = 0
        for timer in due:
            parent = await self._store.get_execution(timer.workflow_id, timer.run_id)
            if parent is None or parent.is_terminal:
                await self._store.remove_timer(timer.id)
                continue
            event = HistoryEvent(
                parent.last_event_id + 1, EventType.TIMER_FIRED, now, {"seq": timer.seq}
            )
            if await self._store.append_events(
                timer.workflow_id, timer.run_id, parent.last_event_id, [event]
            ):
                await self._store.enqueue_workflow_task(
                    WorkflowTask(
                        id=new_id("wft"),
                        workflow_id=timer.workflow_id,
                        run_id=timer.run_id,
                        visible_at=now,
                    )
                )
                fired += 1
            await self._store.remove_timer(timer.id)
        record_timers_fired(fired)
        return fired


def definition_is_async(fn: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


class Worker:
    """Bundles the three processors and drives them as background poll loops.

    For production: ``await worker.run()`` spins the loops until cancelled. For
    tests / the harness: use :meth:`drain` to run until the store quiesces with no
    background tasks (fully deterministic under a manual clock).
    """

    def __init__(
        self,
        store: WorkflowStore,
        workflows: WorkflowRegistry,
        activities: ActivityRegistry,
        *,
        task_queues: Iterable[str] = ("default",),
        clock: Clock | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._config = config or WorkerConfig()
        self.workflow_processor = WorkflowTaskProcessor(
            store, workflows, clock=self._clock, config=self._config
        )
        self.activity_processor = ActivityTaskProcessor(
            store, activities, task_queues=task_queues, clock=self._clock, config=self._config
        )
        self.timer_service = TimerService(store, clock=self._clock)
        self._running = False

    async def step(self) -> bool:
        """Run one unit of available work across all three roles.

        Returns True if anything happened. Fires timers first (so a workflow
        waiting on a now-due timer wakes), then drains workflow tasks (so the
        workflow advances and schedules activities), then runs activities. This
        ordering converges fastest under a manual clock.
        """
        progressed = False
        if await self.timer_service.fire_due():
            progressed = True
        if await self.workflow_processor.process_one():
            progressed = True
        if await self.activity_processor.process_one():
            progressed = True
        return progressed

    async def drain(self, max_steps: int = 10_000) -> None:
        """Run :meth:`step` until no more progress (the store quiesces)."""
        for _ in range(max_steps):
            if not await self.step():
                return
        raise RuntimeError("worker.drain exceeded max_steps (possible livelock)")

    async def run(self) -> None:  # pragma: no cover - real-time loop
        """Production loop: poll until cancelled."""
        self._running = True
        try:
            while self._running:
                if not await self.step():
                    await self._clock.sleep(self._config.poll_interval_s)
        finally:
            self._running = False

    def stop(self) -> None:  # pragma: no cover
        self._running = False


__all__ = [
    "ActivityTaskProcessor",
    "TimerService",
    "Worker",
    "WorkerConfig",
    "WorkflowTaskProcessor",
]
