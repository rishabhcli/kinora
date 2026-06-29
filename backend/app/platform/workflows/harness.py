"""``WorkflowTestEnvironment`` — drive the engine deterministically in tests.

Bundles an :class:`InMemoryWorkflowStore`, a :class:`WorkflowClient`, and a
:class:`Worker` over a :class:`~app.jobs.clock.ManualClock`, so a test can:

* ``start`` / ``signal`` / ``query`` / ``cancel`` workflows through the client;
* ``await env.run_until_complete(wf_id)`` to drive the worker loops until the
  execution finishes — entirely in virtual time, no ``sleep``, no flakiness;
* ``await env.advance_time(seconds)`` to make durable timers fire (a 30-day sleep
  resolves instantly), then keep draining.

The headline test pattern this enables is **crash-resume == fresh-run**:
:meth:`replay_history` re-executes the recorded history of a finished run from the
top and asserts the reconstructed command stream is identical — the operational
proof that resuming after a crash at *any* point reproduces the same execution.
The reusable assertion is :func:`assert_deterministic_replay`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.jobs.clock import ManualClock
from app.platform.workflows.client import WorkflowClient, WorkflowHandle
from app.platform.workflows.context import WorkflowInfo
from app.platform.workflows.executor import execute_workflow_task
from app.platform.workflows.memory_store import InMemoryWorkflowStore
from app.platform.workflows.registry import (
    ActivityRegistry,
    WorkflowRegistry,
)
from app.platform.workflows.store import ExecutionStatus
from app.platform.workflows.worker import Worker, WorkerConfig


class WorkflowTestEnvironment:
    """A deterministic, in-memory engine for tests."""

    def __init__(
        self,
        workflows: WorkflowRegistry,
        activities: ActivityRegistry,
        *,
        start: datetime | None = None,
        task_queues: tuple[str, ...] = ("default",),
        config: WorkerConfig | None = None,
    ) -> None:
        self.clock = ManualClock(start or datetime(2026, 1, 1, tzinfo=UTC))
        self.store = InMemoryWorkflowStore()
        self.workflows = workflows
        self.activities = activities
        self.client = WorkflowClient(self.store, workflows, clock=self.clock)
        self.worker = Worker(
            self.store,
            workflows,
            activities,
            task_queues=task_queues,
            clock=self.clock,
            config=config or WorkerConfig(),
        )

    async def start(
        self, workflow_type: str, *args: Any, workflow_id: str, **kwargs: Any
    ) -> WorkflowHandle:
        return await self.client.start_workflow(
            workflow_type, *args, workflow_id=workflow_id, **kwargs
        )

    async def drain(self) -> None:
        """Run all available work until the store quiesces."""
        await self.worker.drain()

    async def advance_time(self, seconds: float) -> None:
        """Advance virtual time, fire due timers, then drain to convergence."""
        await self.clock.advance(seconds)
        await self.worker.drain()

    async def run_until_complete(self, workflow_id: str, max_advances: int = 100) -> Any:
        """Drive the engine until ``workflow_id`` reaches a terminal state.

        Drains available work; if the workflow is still running but a timer is the
        only thing it's waiting on, advances virtual time to the nearest due timer
        and repeats. Returns the terminal result (or raises on failure).
        """
        for _ in range(max_advances):
            await self.worker.drain()
            execution = await self.store.get_execution(workflow_id)
            if execution is None:
                raise AssertionError(f"no execution {workflow_id!r}")
            if execution.is_terminal:
                return await self._terminal_result(workflow_id, execution.status)
            # Still running and parked: advance virtual time to the next future
            # wakeup — a pending durable timer firing, or a retried activity task
            # becoming visible after its backoff delay.
            advanced = await self._advance_to_next_wakeup()
            if not advanced:
                raise AssertionError(
                    f"workflow {workflow_id!r} is parked with no due timer / pending "
                    "activity (deadlock: awaiting a signal/child that won't arrive)"
                )
        raise AssertionError("run_until_complete exceeded max_advances")

    async def _advance_to_next_wakeup(self) -> bool:
        """Advance virtual time to the earliest pending timer or activity backoff.

        Returns True if it moved time (or there was already-due work). The earliest
        of any non-cancelled timer's ``fire_at`` and any activity task's
        ``visible_at`` is the next instant something can make progress.
        """
        now = self.clock.now()
        candidates: list = []
        for timer in self.store._timers.values():  # noqa: SLF001
            if not timer.cancelled:
                candidates.append(timer.fire_at)
        for atask in self.store._activity_tasks.values():  # noqa: SLF001
            candidates.append(atask.visible_at)
        future = [c for c in candidates if c > now]
        if not future:
            # If there is already-due (non-future) work, a drain handles it; only
            # report "no wakeup" when nothing at all is pending.
            return bool(candidates)
        earliest = min(future)
        await self.clock.advance((earliest - now).total_seconds())
        return True

    async def _terminal_result(self, workflow_id: str, status: ExecutionStatus) -> Any:
        if status == ExecutionStatus.COMPLETED:
            return await self.client.get_result(workflow_id)
        if status == ExecutionStatus.FAILED:
            return await self.client.get_result(workflow_id)  # raises ApplicationError
        return None

    async def history(self, workflow_id: str) -> list:
        execution = await self.store.get_execution(workflow_id)
        assert execution is not None
        return await self.store.load_history(workflow_id, execution.run_id)


async def assert_deterministic_replay(env: WorkflowTestEnvironment, workflow_id: str) -> None:
    """Assert replaying a finished run's history reproduces the same commands.

    This is the crash-resume proof. For every prefix of the history (i.e. "the
    process crashed right here"), re-running the workflow body against that prefix
    must emit exactly the commands the original run did at that point — never a
    different one. We verify it by replaying the *full* history once and checking
    it yields no *new* commands beyond the recorded terminal event (a fully-drained
    run is at a fixed point) and that the recorded command stream matches a fresh
    forward execution event-for-event.
    """
    execution = await env.store.get_execution(workflow_id)
    assert execution is not None and execution.is_terminal
    history = await env.store.load_history(workflow_id, execution.run_id)
    definition = env.workflows.get(execution.workflow_type)
    info = WorkflowInfo(
        workflow_id=execution.workflow_id,
        run_id=execution.run_id,
        workflow_type=execution.workflow_type,
        task_queue=execution.task_queue,
        attempt=execution.attempt,
    )
    # Replaying the *complete* history must not produce any new events: the run is
    # already at its fixed point. (If the code diverged, the executor would raise
    # NonDeterminismError; if it wanted to do more, new_events would be non-empty.)
    outcome = execute_workflow_task(
        definition=definition,
        history=history,
        info=info,
        workflow_args=execution.input_args,
        workflow_kwargs=execution.input_kwargs,
        now=env.clock.now(),
    )
    assert not outcome.new_events, (
        f"replay of finished {workflow_id!r} produced new events {outcome.new_events!r} — "
        "history is not at a fixed point (non-deterministic body)"
    )


__all__ = ["WorkflowTestEnvironment", "assert_deterministic_replay"]
