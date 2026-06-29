"""Child workflows + continue-as-new — composition and history relief."""

from __future__ import annotations

from app.platform.workflows import (
    ChildWorkflowFailure,
    WorkflowRegistry,
    WorkflowTestEnvironment,
    assert_deterministic_replay,
    workflow,
)
from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.registry import ActivityRegistry, activity


async def test_child_workflow_result_returns_to_parent() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="square", registry=areg)
    async def square(actx: ActivityContext, x: int) -> int:
        return x * x

    @workflow(name="child", registry=wreg)
    async def child(ctx: WorkflowContext, x: int) -> int:
        return await ctx.execute_activity("square", x)

    @workflow(name="parent", registry=wreg)
    async def parent(ctx: WorkflowContext, n: int) -> int:
        total = 0
        for i in range(n):
            total += await ctx.start_child_workflow("child", i)
        return total

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("parent", 4, workflow_id="p")
    # 0 + 1 + 4 + 9 = 14
    assert await env.run_until_complete("p") == 14
    await assert_deterministic_replay(env, "p")


async def test_child_failure_surfaces_in_parent() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="failing_child", registry=wreg)
    async def failing_child(ctx: WorkflowContext) -> int:
        raise ValueError("child blew up")

    @workflow(name="catcher", registry=wreg)
    async def catcher(ctx: WorkflowContext) -> str:
        try:
            await ctx.start_child_workflow("failing_child")
            return "no-error"
        except ChildWorkflowFailure as exc:
            return f"caught:{exc.cause.type}"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("catcher", workflow_id="c")
    assert await env.run_until_complete("c") == "caught:ValueError"


async def test_continue_as_new_keeps_history_bounded() -> None:
    """A long loop continues-as-new each iteration; the final result carries state."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="accumulate", registry=wreg)
    async def accumulate(ctx: WorkflowContext, target: int, acc: int = 0, step: int = 0) -> int:
        if step >= target:
            return acc
        # do a unit of work, then continue-as-new with the carried accumulator
        acc += step
        ctx.continue_as_new(target, acc, step + 1)
        return -1  # unreachable

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("accumulate", 5, workflow_id="acc")
    # 0+1+2+3+4 = 10
    assert await env.run_until_complete("acc") == 10
    # Each run's history stays tiny (it never accumulates across the loop).
    execution = await env.store.get_execution("acc")
    assert execution is not None
    final_history = await env.store.load_history("acc", execution.run_id)
    assert len(final_history) <= 4  # started + completed (+ a marker or two)


async def test_continue_as_new_preserves_workflow_id() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="rollover", registry=wreg)
    async def rollover(ctx: WorkflowContext, rounds: int, done: int = 0) -> dict:
        if done >= rounds:
            return {"workflow_id": ctx.info.workflow_id, "done": done}
        ctx.continue_as_new(rounds, done + 1)
        return {}

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("rollover", 3, workflow_id="ro")
    result = await env.run_until_complete("ro")
    assert result == {"workflow_id": "ro", "done": 3}
