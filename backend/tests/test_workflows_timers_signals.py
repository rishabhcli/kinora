"""Durable timers, signals, queries, and cancellation."""

from __future__ import annotations

from app.platform.workflows import (
    WorkflowRegistry,
    WorkflowTestEnvironment,
    wait_any,
    workflow,
)
from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.registry import ActivityRegistry, activity


async def test_durable_timer_fires_in_virtual_time() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="sleeper", registry=wreg)
    async def sleeper(ctx: WorkflowContext) -> str:
        await ctx.sleep(3600)  # an hour — free under virtual time
        return "awake"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("sleeper", workflow_id="s")
    assert await env.run_until_complete("s") == "awake"


async def test_signal_delivers_payload_into_workflow() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="waiter", registry=wreg)
    async def waiter(ctx: WorkflowContext) -> str:
        payload = await ctx.wait_for_signal("go")
        return f"got:{payload}"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("waiter", workflow_id="w")
    await env.worker.drain()  # parks waiting for the signal
    parked = await env.store.get_execution("w")
    assert parked is not None and parked.status.value == "running"
    await env.client.signal_workflow("w", "go", "hello")
    assert await env.run_until_complete("w") == "got:hello"


async def test_multiple_signals_delivered_in_order() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="collector", registry=wreg)
    async def collector(ctx: WorkflowContext) -> list:
        out = []
        for _ in range(3):
            out.append(await ctx.wait_for_signal("item"))
        return out

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("collector", workflow_id="c")
    await env.worker.drain()
    for i in range(3):
        await env.client.signal_workflow("c", "item", i)
        await env.worker.drain()
    assert await env.run_until_complete("c") == [0, 1, 2]


async def test_query_reads_current_state_without_mutation() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="counter", registry=wreg)
    async def counter(ctx: WorkflowContext) -> int:
        state = {"n": 0}
        ctx.register_query("count", lambda: state["n"])
        for _ in range(2):
            await ctx.wait_for_signal("tick")
            state["n"] += 1
        return state["n"]

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("counter", workflow_id="q")
    await env.worker.drain()
    assert await env.client.query_workflow("q", "count") == 0
    await env.client.signal_workflow("q", "tick")
    await env.worker.drain()
    assert await env.client.query_workflow("q", "count") == 1
    # Querying didn't append events / advance the run.
    execution = await env.store.get_execution("q")
    assert execution is not None and not execution.is_terminal


async def test_timer_races_signal_via_wait_any() -> None:
    """A signal that arrives before the timer wins the race deterministically."""
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="racer", registry=wreg)
    async def racer(ctx: WorkflowContext) -> str:
        timer = ctx.start_timer(1000)
        signal = ctx.wait_for_signal("decision")
        index, value = await wait_any(signal, timer)
        return "signal" if index == 0 else "timeout"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("racer", workflow_id="rc")
    await env.worker.drain()
    await env.client.signal_workflow("rc", "decision", "x")
    assert await env.run_until_complete("rc") == "signal"


async def test_timer_wins_when_no_signal() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @workflow(name="racer2", registry=wreg)
    async def racer2(ctx: WorkflowContext) -> str:
        timer = ctx.start_timer(60)
        signal = ctx.wait_for_signal("decision")
        index, _ = await wait_any(signal, timer)
        return "signal" if index == 0 else "timeout"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("racer2", workflow_id="rc2")
    # No signal ever sent; advancing time fires the timer.
    assert await env.run_until_complete("rc2") == "timeout"


async def test_cancellation_observed_by_workflow() -> None:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()

    @activity(name="noop", registry=areg)
    async def noop(actx: ActivityContext) -> str:
        return "ok"

    @workflow(name="cancellable", registry=wreg)
    async def cancellable(ctx: WorkflowContext) -> str:
        # A heartbeat loop: each iteration awaits a durable timer, then checks for
        # cancellation *after* the await (so the timer it started always resolves
        # — never abandoned mid-history, which would be non-determinism). On a
        # cancel request the loop exits and runs compensation cleanly.
        for _ in range(100):
            await ctx.sleep(10)
            if ctx.is_cancelled:
                return "cancelled-cleanly"
        return "ran-out"

    env = WorkflowTestEnvironment(wreg, areg)
    await env.start("cancellable", workflow_id="cn")
    await env.worker.drain()
    await env.client.cancel_workflow("cn")
    assert await env.run_until_complete("cn") == "cancelled-cleanly"
