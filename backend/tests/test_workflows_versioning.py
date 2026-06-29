"""Versioning / patching — deploying new code against in-flight histories."""

from __future__ import annotations

from datetime import UTC, datetime

from app.platform.workflows import (
    WorkflowRegistry,
    WorkflowTestEnvironment,
    workflow,
)
from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.context import WorkflowContext, WorkflowInfo
from app.platform.workflows.executor import execute_workflow_task
from app.platform.workflows.registry import ActivityRegistry, activity


async def test_new_run_pins_max_version() -> None:
    """A fresh run records the newest supported version for a change id."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="newstep", registry=areg)
    async def newstep(actx: ActivityContext) -> str:
        return "v2-path"

    @activity(name="oldstep", registry=areg)
    async def oldstep(actx: ActivityContext) -> str:
        return "v1-path"

    @workflow(name="versioned", registry=wreg)
    async def versioned(ctx: WorkflowContext) -> str:
        version = ctx.get_version("add-newstep", -1, 1)
        if version >= 1:
            return await ctx.execute_activity("newstep")
        return await ctx.execute_activity("oldstep")

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("versioned", workflow_id="v")
    # A fresh run takes the new branch (max version pinned).
    assert await env.run_until_complete("v") == "v2-path"


async def test_old_history_keeps_old_branch_on_replay() -> None:
    """An in-flight run that predates the change keeps the old branch on replay.

    We simulate this by hand-building a history that has *no* version marker for
    the change (an old run), then replaying new code that supports the change:
    ``get_version`` returns DEFAULT_VERSION (-1) so the old branch runs.
    """
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="newstep", registry=areg)
    async def newstep(actx: ActivityContext) -> str:
        return "v2-path"

    @activity(name="oldstep", registry=areg)
    async def oldstep(actx: ActivityContext) -> str:
        return "v1-path"

    @workflow(name="versioned", registry=wreg)
    async def versioned(ctx: WorkflowContext) -> str:
        version = ctx.get_version("add-newstep", -1, 1)
        if version >= 1:
            return await ctx.execute_activity("newstep")
        return await ctx.execute_activity("oldstep")

    from app.platform.workflows.events import EventType, HistoryEvent

    now = datetime(2026, 1, 1, tzinfo=UTC)
    # An OLD history: started, then directly scheduled "oldstep" at seq 2 (no
    # version marker, because the old code didn't call get_version). Note the
    # version marker would have been seq 1 had the new code run fresh; the old
    # run never recorded it, so get_version yields DEFAULT_VERSION.
    history = [
        HistoryEvent(1, EventType.WORKFLOW_STARTED, now, {"args": [], "kwargs": {}}),
        HistoryEvent(
            2,
            EventType.ACTIVITY_SCHEDULED,
            now,
            {"seq": 1, "activity_type": "oldstep", "args": [], "kwargs": {}},
        ),
    ]
    info = WorkflowInfo(
        workflow_id="old", run_id="r", workflow_type="versioned", task_queue="default", attempt=1
    )
    # Replaying the NEW code against the OLD history must NOT diverge: get_version
    # returns -1 (no recorded marker), so the old "oldstep" branch is taken,
    # matching the recorded ACTIVITY_SCHEDULED at seq 2.
    outcome = execute_workflow_task(
        definition=wreg.get("versioned"),
        history=history,
        info=info,
        workflow_args=[],
        workflow_kwargs={},
        now=now,
    )
    # No divergence raised, and it parks awaiting the oldstep completion.
    assert outcome.new_events == []


async def test_patched_boolean_sugar() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="patchwf", registry=wreg)
    async def patchwf(ctx: WorkflowContext) -> bool:
        return ctx.patched("my-change")

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("patchwf", workflow_id="pt")
    # A fresh run is patched (new behaviour).
    assert await env.run_until_complete("pt") is True
