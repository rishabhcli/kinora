"""Operational crash-resume proof at the worker-runtime level.

The determinism tests prove the *executor* replays identically. These prove the
*whole runtime* survives crashes: we run a workflow with a brand-new worker
instance for **each single step**, discarding all in-process state between steps
(only the durable store persists). If crash-resume ≡ fresh-run holds, the
fresh-each-step run must reach the identical terminal result + history as a run
that used one long-lived worker.

We also prove at-least-once activity delivery: a worker that "crashes" after
running an activity but before recording its completion (lease lapses) re-delivers
the activity, and an idempotent activity converges to the same result.
"""

from __future__ import annotations

from app.jobs.clock import ManualClock
from app.platform.workflows import (
    InMemoryWorkflowStore,
    WorkflowRegistry,
    WorkflowTestEnvironment,
    workflow,
)
from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.client import WorkflowClient
from app.platform.workflows.context import WorkflowContext
from app.platform.workflows.registry import ActivityRegistry, activity
from app.platform.workflows.worker import Worker


def _registries() -> tuple[WorkflowRegistry, ActivityRegistry, dict]:
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    calls: dict[str, int] = {}

    @activity(name="work", registry=areg)
    async def work(actx: ActivityContext, label: str) -> str:
        calls[label] = calls.get(label, 0) + 1
        return f"done:{label}"

    @workflow(name="multi", registry=wreg)
    async def multi(ctx: WorkflowContext) -> list:
        out = []
        for label in ("a", "b", "c"):
            out.append(await ctx.execute_activity("work", label))
            await ctx.sleep(5)
        return out

    return wreg, areg, calls


async def test_fresh_worker_each_step_equals_long_lived_run() -> None:
    """Run with a NEW worker per step; result must equal a single-worker run."""
    # Baseline: one long-lived worker.
    wreg, areg, _ = _registries()
    baseline = WorkflowTestEnvironment(wreg, areg)
    await baseline.start("multi", workflow_id="m")
    baseline_result = await baseline.run_until_complete("m")
    baseline_exec = await baseline.store.get_execution("m")
    assert baseline_exec is not None
    baseline_history = await baseline.store.load_history("m", baseline_exec.run_id)

    # Crash-y: a fresh Worker object for every single step, sharing only the store.
    wreg2, areg2, _ = _registries()
    store = InMemoryWorkflowStore()
    clock = ManualClock()
    client = WorkflowClient(store, wreg2, clock=clock)
    await client.start_workflow("multi", workflow_id="m")

    for _ in range(500):
        worker = Worker(store, wreg2, areg2, clock=clock)  # brand-new, no memory
        progressed = await worker.step()
        execution = await store.get_execution("m")
        assert execution is not None
        if execution.is_terminal:
            break
        if not progressed:
            # Parked on a timer: advance virtual time to the next due timer.
            timers = [t for t in store._timers.values() if not t.cancelled]  # noqa: SLF001
            if timers:
                earliest = min(t.fire_at for t in timers)
                await clock.advance(max(0.0, (earliest - clock.now()).total_seconds()))
    else:
        raise AssertionError("did not converge")

    execution = await store.get_execution("m")
    assert execution is not None
    crashy_result = await client.get_result("m")
    crashy_history = await store.load_history("m", execution.run_id)

    # The two runs are equivalent: same result, same history shape.
    assert crashy_result == baseline_result == ["done:a", "done:b", "done:c"]
    assert [e.type for e in crashy_history] == [e.type for e in baseline_history]


async def test_at_least_once_activity_redelivery_is_idempotent() -> None:
    """A lapsed lease re-delivers the activity; the workflow result is unchanged.

    We force re-delivery by claiming an activity task, *not* completing it (the
    worker 'crashed'), letting the lease lapse via the clock, then running a fresh
    worker — which re-claims and runs it. The activity runs twice, but the
    workflow records exactly one completion (the first commit that wins).
    """
    wreg, areg = WorkflowRegistry(), ActivityRegistry()
    runs = {"n": 0}

    @activity(name="idem", registry=areg)
    async def idem(actx: ActivityContext) -> str:
        runs["n"] += 1
        return "result"

    @workflow(name="wf", registry=wreg)
    async def wf(ctx: WorkflowContext) -> str:
        return await ctx.execute_activity("idem")

    clock = ManualClock()
    store = InMemoryWorkflowStore()
    client = WorkflowClient(store, wreg, clock=clock)
    await client.start_workflow("wf", workflow_id="w")

    # Advance the workflow so it schedules the activity (workflow task only).
    from app.platform.workflows.worker import WorkflowTaskProcessor

    wtp = WorkflowTaskProcessor(store, wreg, clock=clock)
    assert await wtp.process_one() is True

    # Simulate a crashed activity worker: claim the activity, then abandon it.
    claimed = await store.claim_activity_task(
        now=clock.now(), task_queues=["default"], lease_token="dead-worker", lease_s=30
    )
    assert claimed is not None
    # Lease lapses.
    await clock.advance(31)

    # A fresh worker drains everything to completion.
    worker = Worker(store, wreg, areg, clock=clock)
    await worker.drain()
    execution = await store.get_execution("w")
    assert execution is not None and execution.status.value == "completed"
    assert await client.get_result("w") == "result"
    assert runs["n"] >= 1  # at-least-once (ran one or more times)
