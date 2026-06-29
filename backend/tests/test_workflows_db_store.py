"""Postgres-durable store tests (require the isolated test DB).

Re-run the store contract against :class:`PostgresWorkflowStore` and prove a full
workflow runs end-to-end through the durable backend — including a crash-resume
where a *fresh worker per step* shares only the Postgres store. The conftest
``_isolate_state`` fixture ensures the schema (``create_all`` now includes the
``workflow_*`` tables) and truncates between tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.composition import make_session_factory
from app.jobs.clock import ManualClock
from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.client import WorkflowClient
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.db_store import PostgresWorkflowStore
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.ids import new_id
from app.platform.workflows.registry import ActivityRegistry, WorkflowRegistry, activity, workflow
from app.platform.workflows.store import (
    ExecutionStatus,
    WorkflowExecution,
    WorkflowTask,
)
from app.platform.workflows.worker import Worker

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping workflow postgres store tests"
)


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PostgresWorkflowStore]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    factory = make_session_factory(maker)
    try:
        yield PostgresWorkflowStore(factory)
    finally:
        await engine.dispose()


def _exec() -> WorkflowExecution:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return WorkflowExecution(
        workflow_id="wf",
        run_id="run1",
        workflow_type="t",
        task_queue="default",
        status=ExecutionStatus.RUNNING,
        input_args=[1, "x"],
        input_kwargs={"k": 2},
        created_at=now,
        updated_at=now,
    )


async def test_create_load_and_history(store: PostgresWorkflowStore) -> None:
    await store.create_execution(_exec())
    loaded = await store.get_execution("wf")
    assert loaded is not None
    assert loaded.input_args == [1, "x"]
    assert loaded.input_kwargs == {"k": 2}

    now = datetime(2026, 1, 1, tzinfo=UTC)
    e1 = HistoryEvent(1, EventType.WORKFLOW_STARTED, now, {"args": [1], "kwargs": {}})
    assert await store.append_events("wf", "run1", 0, [e1]) is True
    history = await store.load_history("wf", "run1")
    assert [e.type for e in history] == [EventType.WORKFLOW_STARTED]


async def test_optimistic_concurrency_rejects_stale_append(store: PostgresWorkflowStore) -> None:
    await store.create_execution(_exec())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    e1 = HistoryEvent(1, EventType.WORKFLOW_STARTED, now, {})
    assert await store.append_events("wf", "run1", 0, [e1]) is True
    e2 = HistoryEvent(2, EventType.TIMER_STARTED, now, {"seq": 1})
    # Stale appender (expected_last_event_id still 0) is rejected.
    assert await store.append_events("wf", "run1", 0, [e2]) is False
    assert await store.append_events("wf", "run1", 1, [e2]) is True


async def test_activity_task_lease_and_relapse(store: PostgresWorkflowStore) -> None:
    from app.platform.workflows.store import ActivityTask

    await store.create_execution(_exec())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    task = ActivityTask(
        id=new_id("act"),
        workflow_id="wf",
        run_id="run1",
        seq=1,
        activity_type="a",
        args=[],
        kwargs={},
        task_queue="default",
        attempt=0,
        retry_policy_dict=None,
        start_to_close_timeout_s=None,
        schedule_to_close_timeout_s=None,
        heartbeat_timeout_s=None,
        visible_at=now,
        scheduled_at=now,
    )
    await store.enqueue_activity_task(task)
    claimed = await store.claim_activity_task(
        now=now, task_queues=["default"], lease_token="A", lease_s=30
    )
    assert claimed is not None
    # Held under lease.
    assert (
        await store.claim_activity_task(
            now=now, task_queues=["default"], lease_token="B", lease_s=30
        )
        is None
    )
    # Relapses after the lease window.
    later = now + timedelta(seconds=31)
    again = await store.claim_activity_task(
        now=later, task_queues=["default"], lease_token="C", lease_s=30
    )
    assert again is not None


async def test_full_workflow_runs_on_postgres(store: PostgresWorkflowStore) -> None:
    """An end-to-end workflow (activities + timer) over the durable Postgres store."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="inc", registry=areg)
    async def inc(actx: ActivityContext, x: int) -> int:
        return x + 1

    @workflow(name="chain", registry=wreg)
    async def chain(ctx: WorkflowContext, n: int) -> int:
        v = 0
        for _ in range(n):
            v = await ctx.execute_activity("inc", v)
        await ctx.sleep(5)
        return v

    clock = ManualClock()
    client = WorkflowClient(store, wreg, clock=clock)
    await client.start_workflow("chain", 3, workflow_id="pgwf")

    # Fresh worker per step (only the durable store persists) → crash-resume proof.
    for _ in range(200):
        worker = Worker(store, wreg, areg, clock=clock)
        progressed = await worker.step()
        execution = await store.get_execution("pgwf")
        assert execution is not None
        if execution.is_terminal:
            break
        if not progressed:
            due = await store.due_timers(clock.now() + timedelta(days=1))
            if due:
                await clock.advance(
                    max(0.0, (min(t.fire_at for t in due) - clock.now()).total_seconds())
                )
    assert await client.get_result("pgwf") == 3


async def test_enqueue_workflow_task_dedups(store: PostgresWorkflowStore) -> None:
    await store.create_execution(_exec())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(3):
        await store.enqueue_workflow_task(
            WorkflowTask(id=new_id("wft"), workflow_id="wf", run_id="run1", visible_at=now)
        )
    stats = await store.stats()
    assert stats.pending_workflow_tasks == 1
