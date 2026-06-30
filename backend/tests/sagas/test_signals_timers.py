"""Signals / external-event awaiting + await-timeout routing."""

from __future__ import annotations

import pytest

from app.sagas import (
    FakeClock,
    InMemoryDurableStore,
    RecordingBus,
    RunStatus,
    SagaEngine,
    SagaEventType,
    SagaFailed,
    Step,
    StepContext,
    StepStatus,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from tests.sagas.helpers import AdvancingSleeper, Recorder, record_of, seq_run_ids


def _engine(
    wf: Workflow, clock: FakeClock | None = None, bus: RecordingBus | None = None
) -> tuple[SagaEngine, InMemoryDurableStore, FakeClock, RecordingBus]:
    clock = clock or FakeClock()
    store = InMemoryDurableStore()
    bus = bus or RecordingBus()
    engine = SagaEngine(
        WorkflowRegistry([wf]),
        store,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus,
        run_id_factory=seq_run_ids(),
    )
    return engine, store, clock, bus


async def test_await_signal_parks_then_resumes_on_delivery() -> None:
    rec = Recorder()

    async def before(ctx: StepContext) -> str:
        rec.add("before")
        return "b"

    async def on_event(ctx: StepContext) -> str:
        rec.add("on_event")
        # the delivered payload is exposed on the context
        assert ctx.signal_payload == {"approved": True}
        return "handled"

    async def after(ctx: StepContext) -> str:
        rec.add("after")
        return "a"

    wf = Workflow(
        name="awaiter",
        steps=(
            Step("before", before),
            Step("on_event", on_event, await_signal="approval"),
            Step("after", after),
        ),
    )
    engine, store, clock, bus = _engine(wf)
    # Run parks WAITING on the approval signal after `before`.
    state = await engine.start("awaiter", run_id="S1")
    assert state.status == RunStatus.WAITING
    assert state.timer is not None and state.timer.signal == "approval"
    assert rec.ops() == ["before"]  # on_event not yet run
    assert SagaEventType.SIGNAL_WAIT in bus.types()

    # Delivering the signal resumes the run to completion.
    final = await engine.signal("S1", "approval", {"approved": True})
    assert final.status == RunStatus.COMPLETED
    assert rec.ops() == ["before", "on_event", "after"]
    # the consumed signal is cleared from pending after the step succeeds
    assert "approval" not in final.pending_signals


async def test_signal_delivered_before_await_is_consumed() -> None:
    """A signal that arrives before the step reaches its await is stashed and
    consumed when the step runs — no lost wakeup."""
    rec = Recorder()

    async def slow(ctx: StepContext) -> str:
        rec.add("slow")
        return "s"

    async def gated(ctx: StepContext) -> str:
        rec.add("gated")
        assert ctx.signal_payload == "early"
        return "g"

    wf = Workflow(
        name="early",
        steps=(Step("slow", slow), Step("gated", gated, await_signal="go")),
    )
    engine, store, clock, bus = _engine(wf)
    # Start parks on `gated`'s await.
    await engine.start("early", run_id="S2")
    # deliver — resumes and consumes
    final = await engine.signal("S2", "go", "early")
    assert final.status == RunStatus.COMPLETED
    assert rec.ops() == ["slow", "gated"]


async def test_await_timeout_routes_to_branch_target() -> None:
    rec = Recorder()

    async def wait_step(ctx: StepContext) -> str:
        rec.add("wait_step")  # should NOT run — signal never arrives
        return "w"

    async def fallback(ctx: StepContext) -> str:
        rec.add("fallback")
        return "f"

    wf = Workflow(
        name="timeoutroute",
        steps=(
            Step(
                "wait_step",
                wait_step,
                await_signal="never",
                await_timeout_s=10.0,
                on_await_timeout="fallback",
            ),
            Step("fallback", fallback),
        ),
    )
    clock = FakeClock()
    engine, store, clock, bus = _engine(wf, clock=clock)
    state = await engine.start("timeoutroute", run_id="S3")
    assert state.status == RunStatus.WAITING

    # Before the deadline, resuming keeps it parked.
    clock.advance(5.0)
    still = await engine.resume("S3")
    assert still.status == RunStatus.WAITING
    assert rec.ops() == []

    # After the deadline, resume routes to the fallback branch.
    clock.advance(6.0)  # now t=11 > fire_at=10
    final = await engine.resume("S3")
    assert final.status == RunStatus.COMPLETED
    assert rec.ops() == ["fallback"]  # wait_step body never ran
    assert record_of(final, "wait_step").status == StepStatus.SKIPPED


async def test_await_timeout_without_route_fails_and_compensates() -> None:
    rec = Recorder()

    async def setup(ctx: StepContext) -> str:
        rec.add("setup")
        return "s"

    async def undo_setup(ctx: StepContext) -> None:
        rec.add("undo_setup")

    async def gate(ctx: StepContext) -> None:
        rec.add("gate")  # never runs

    wf = Workflow(
        name="hardtimeout",
        steps=(
            Step("setup", setup, compensation=undo_setup),
            Step("gate", gate, await_signal="never", await_timeout_s=5.0),
        ),
    )
    clock = FakeClock()
    engine, store, clock, bus = _engine(wf, clock=clock)
    await engine.start("hardtimeout", run_id="S4")
    clock.advance(6.0)
    with pytest.raises(SagaFailed):
        await engine.resume("S4")
    final = await store.load("S4")
    assert final.status == RunStatus.FAILED
    # setup was compensated when the await timed out with no route.
    assert rec.ops() == ["setup", "undo_setup"]
    assert final.compensated == ["setup"]


async def test_signal_to_completed_run_is_noop() -> None:
    async def s(ctx: StepContext) -> None:
        return None

    wf = Workflow(name="done", steps=(Step("s", s),))
    engine, store, clock, bus = _engine(wf)
    st = await engine.start("done", run_id="S5")
    assert st.status == RunStatus.COMPLETED
    again = await engine.signal("S5", "whatever")
    assert again.status == RunStatus.COMPLETED
    assert again.revision == st.revision
