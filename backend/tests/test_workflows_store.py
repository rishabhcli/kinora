"""In-memory store contract + client semantics (zero infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.platform.workflows import (
    WorkflowAlreadyExistsError,
    WorkflowNotFoundError,
    WorkflowRegistry,
    WorkflowTestEnvironment,
    workflow,
)
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.ids import new_id
from app.platform.workflows.memory_store import InMemoryWorkflowStore
from app.platform.workflows.registry import ActivityRegistry
from app.platform.workflows.store import (
    ExecutionStatus,
    WorkflowExecution,
    WorkflowTask,
)


def _exec(workflow_id: str = "wf", run_id: str = "run") -> WorkflowExecution:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return WorkflowExecution(
        workflow_id=workflow_id,
        run_id=run_id,
        workflow_type="t",
        task_queue="default",
        status=ExecutionStatus.RUNNING,
        input_args=[],
        input_kwargs={},
        created_at=now,
        updated_at=now,
    )


async def test_append_events_optimistic_concurrency() -> None:
    store = InMemoryWorkflowStore()
    execution = _exec()
    await store.create_execution(execution)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    e1 = HistoryEvent(1, EventType.WORKFLOW_STARTED, now, {})
    # First append from last_event_id=0 succeeds.
    assert await store.append_events("wf", "run", 0, [e1]) is True
    # A stale appender (still thinks last_event_id is 0) is rejected.
    e2 = HistoryEvent(2, EventType.TIMER_STARTED, now, {"seq": 1})
    assert await store.append_events("wf", "run", 0, [e2]) is False
    # The correct appender (last_event_id=1) succeeds.
    assert await store.append_events("wf", "run", 1, [e2]) is True
    history = await store.load_history("wf", "run")
    assert [e.event_id for e in history] == [1, 2]


async def test_workflow_task_lease_and_visibility() -> None:
    store = InMemoryWorkflowStore()
    await store.create_execution(_exec())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await store.enqueue_workflow_task(
        WorkflowTask(id=new_id("wft"), workflow_id="wf", run_id="run", visible_at=now)
    )
    # First claim leases it.
    claimed = await store.claim_workflow_task(now=now, lease_token="A", lease_s=30)
    assert claimed is not None
    # A second claim within the lease window finds nothing.
    assert await store.claim_workflow_task(now=now, lease_token="B", lease_s=30) is None
    # After the lease lapses it is claimable again (at-least-once).
    later = now + timedelta(seconds=31)
    reclaimed = await store.claim_workflow_task(now=later, lease_token="C", lease_s=30)
    assert reclaimed is not None and reclaimed.id == claimed.id


async def test_workflow_task_dedup_per_run() -> None:
    store = InMemoryWorkflowStore()
    await store.create_execution(_exec())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(3):
        await store.enqueue_workflow_task(
            WorkflowTask(id=new_id("wft"), workflow_id="wf", run_id="run", visible_at=now)
        )
    stats = await store.stats()
    assert stats.pending_workflow_tasks == 1  # collapsed to one outstanding task


async def test_timer_due_and_cancel() -> None:
    from app.platform.workflows.store import DurableTimer

    store = InMemoryWorkflowStore()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fire = now + timedelta(seconds=10)
    await store.add_timer(
        DurableTimer(id="t1", workflow_id="wf", run_id="run", seq=1, fire_at=fire)
    )
    assert await store.due_timers(now) == []  # not yet due
    assert len(await store.due_timers(fire)) == 1  # due at fire time
    await store.cancel_timer("wf", "run", 1)
    assert await store.due_timers(fire) == []  # cancelled timers never fire


async def test_client_start_is_idempotent_while_open() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="noop", registry=wreg)
    async def noop(ctx: WorkflowContext) -> str:
        await ctx.wait_for_signal("never")
        return "x"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("noop", workflow_id="dup")
    await env.worker.drain()
    # Starting the same still-open workflow id again is rejected.
    try:
        await env.start("noop", workflow_id="dup")
        raise AssertionError("expected WorkflowAlreadyExistsError")
    except WorkflowAlreadyExistsError:
        pass


async def test_client_signal_unknown_workflow_raises() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    env = WorkflowTestEnvironment(wreg, areg)
    try:
        await env.client.signal_workflow("missing", "sig", None)
        raise AssertionError("expected WorkflowNotFoundError")
    except WorkflowNotFoundError:
        pass


async def test_list_executions_filters_by_status() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="quick", registry=wreg)
    async def quick(ctx: WorkflowContext) -> str:
        return "ok"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("quick", workflow_id="a")
    await env.start("quick", workflow_id="b")
    await env.run_until_complete("a")
    await env.run_until_complete("b")
    completed = await env.store.list_executions(status=ExecutionStatus.COMPLETED)
    assert {e.workflow_id for e in completed} == {"a", "b"}
