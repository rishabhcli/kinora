"""Activity-level tests — at-least-once, retries, timeouts, heartbeat, cancel.

Activities are the non-deterministic I/O units; the engine makes them durable via
retries (consulting the :class:`RetryPolicy`), start-to-close timeouts,
heartbeating (lease renewal + progress checkpointing), and cancellation. These
tests drive each path through the in-memory environment.
"""

from __future__ import annotations

from app.platform.workflows import (
    ActivityContext,
    ActivityFailure,
    ApplicationError,
    RetryPolicy,
    WorkflowRegistry,
    WorkflowTestEnvironment,
    workflow,
)
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.registry import ActivityRegistry, activity


async def test_activity_result_flows_back_into_workflow() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="echo", registry=areg)
    async def echo(actx: ActivityContext, msg: str) -> str:
        return msg.upper()

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext, msg: str) -> str:
        return await ctx.execute_activity("echo", msg)

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", "hi", workflow_id="a")
    assert await env.run_until_complete("a") == "HI"


async def test_activity_retries_then_succeeds() -> None:
    """A flaky activity is retried per the policy until it succeeds."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    attempts = {"n": 0}

    @activity(
        name="flaky",
        retry_policy=RetryPolicy(initial_interval_s=1.0, maximum_attempts=5),
        registry=areg,
    )
    async def flaky(actx: ActivityContext) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ApplicationError("transient", type="Transient")
        return "ok"

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        return await ctx.execute_activity("flaky")

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="r")
    assert await env.run_until_complete("r") == "ok"
    assert attempts["n"] == 3  # failed twice, succeeded on the third


async def test_activity_exhausts_retries_surfaces_failure() -> None:
    """When retries are exhausted the workflow sees a catchable ActivityFailure."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(
        name="always_fails",
        retry_policy=RetryPolicy(initial_interval_s=1.0, maximum_attempts=2),
        registry=areg,
    )
    async def always_fails(actx: ActivityContext) -> str:
        raise ApplicationError("boom", type="Boom")

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        try:
            return await ctx.execute_activity("always_fails")
        except ActivityFailure as exc:
            return f"compensated:{exc.cause.type}"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="f")
    assert await env.run_until_complete("f") == "compensated:Boom"


async def test_non_retryable_error_short_circuits_retries() -> None:
    """A non-retryable error fails the activity immediately, ignoring attempts left."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    attempts = {"n": 0}

    @activity(
        name="hard_fail",
        retry_policy=RetryPolicy(initial_interval_s=1.0, maximum_attempts=10),
        registry=areg,
    )
    async def hard_fail(actx: ActivityContext) -> str:
        attempts["n"] += 1
        raise ApplicationError("nope", type="Fatal", non_retryable=True)

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        try:
            return await ctx.execute_activity("hard_fail")
        except ActivityFailure:
            return "failed"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="h")
    assert await env.run_until_complete("h") == "failed"
    assert attempts["n"] == 1  # never retried


async def test_activity_timeout_surfaces_as_catchable() -> None:
    """A start-to-close timeout surfaces inside the workflow as ActivityTimeout."""
    import asyncio

    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="slow", start_to_close_timeout_s=0.01, registry=areg)
    async def slow(actx: ActivityContext) -> str:
        await asyncio.sleep(1.0)  # exceeds the 10ms timeout
        return "never"

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        from app.platform.workflows.errors import ActivityTimeout

        try:
            return await ctx.execute_activity("slow")
        except ActivityTimeout as exc:
            return f"timed_out:{exc.kind}"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="t")
    assert await env.run_until_complete("t") == "timed_out:start_to_close"


async def test_activity_heartbeat_renews_lease() -> None:
    """An activity can heartbeat to renew its lease and report progress."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    seen: list = []

    @activity(name="long", registry=areg)
    async def long(actx: ActivityContext) -> str:
        ok = await actx.heartbeat({"progress": 0.5})
        seen.append(ok)
        return "done"

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        return await ctx.execute_activity("long")

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="hb")
    assert await env.run_until_complete("hb") == "done"
    assert seen == [True]  # the heartbeat held the lease


async def test_plain_exception_becomes_application_error() -> None:
    """A bare ``raise`` in an activity is captured as an ApplicationError."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(
        name="raises",
        retry_policy=RetryPolicy(initial_interval_s=1.0, maximum_attempts=1),
        registry=areg,
    )
    async def raises(actx: ActivityContext) -> str:
        raise ValueError("kaboom")

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        try:
            return await ctx.execute_activity("raises")
        except ActivityFailure as exc:
            return exc.cause.type or "?"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("wf", workflow_id="e")
    assert await env.run_until_complete("e") == "ValueError"
