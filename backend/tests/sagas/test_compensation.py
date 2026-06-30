"""Saga compensation: completed steps unwind in reverse, best-effort, recorded."""

from __future__ import annotations

import pytest

from app.sagas import (
    NO_RETRY,
    Action,
    Compensation,
    CompensationOutcome,
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
    TransientStepError,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from tests.sagas.helpers import AdvancingSleeper, Recorder, record_of, seq_run_ids


def _engine(
    wf: Workflow, bus: RecordingBus | None = None
) -> tuple[SagaEngine, InMemoryDurableStore, RecordingBus]:
    clock = FakeClock()
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
    return engine, store, bus


def _make_step(
    rec: Recorder, name: str, *, fail: bool = False
) -> tuple[Action, Compensation]:
    async def action(ctx: StepContext) -> str:
        rec.add(f"do:{name}")
        if fail:
            raise TransientStepError(f"{name} failed")
        return name

    async def comp(ctx: StepContext) -> None:
        rec.add(f"undo:{name}")

    return action, comp


async def test_failure_compensates_completed_steps_in_reverse() -> None:
    rec = Recorder()
    a_do, a_undo = _make_step(rec, "a")
    b_do, b_undo = _make_step(rec, "b")
    c_do, c_undo = _make_step(rec, "c", fail=True)  # c fails past retries

    wf = Workflow(
        name="comp",
        steps=(
            Step("a", a_do, compensation=a_undo, retry=NO_RETRY),
            Step("b", b_do, compensation=b_undo, retry=NO_RETRY),
            Step("c", c_do, compensation=c_undo, retry=NO_RETRY),
        ),
    )
    engine, store, bus = _engine(wf)
    with pytest.raises(SagaFailed) as ei:
        await engine.start("comp", run_id="C1")

    # forward a, b; c fails; then undo b, undo a (reverse). c never completed so
    # its own compensation does not run.
    assert rec.ops() == ["do:a", "do:b", "do:c", "undo:b", "undo:a"]
    final = await store.load("C1")
    assert final.status == RunStatus.FAILED
    assert final.failed_step == "c"
    assert final.compensated == ["b", "a"]  # reverse order recorded
    assert record_of(final, "a").compensation == CompensationOutcome.OK
    assert record_of(final, "b").compensation == CompensationOutcome.OK
    assert record_of(final, "a").status == StepStatus.COMPENSATED
    assert record_of(final, "c").compensation == CompensationOutcome.NONE
    # the SagaFailed post-mortem carries the unwind result
    assert ei.value.compensated == ["b", "a"]
    assert ei.value.compensation_failures == []
    # telemetry: two compensations, in reverse
    assert bus.steps_of(SagaEventType.COMPENSATION_OK) == ["b", "a"]


async def test_compensation_is_best_effort_and_records_failures() -> None:
    rec = Recorder()

    async def a_do(ctx: StepContext) -> str:
        rec.add("do:a")
        return "a"

    async def a_undo(ctx: StepContext) -> None:
        rec.add("undo:a")  # this one succeeds

    async def b_do(ctx: StepContext) -> str:
        rec.add("do:b")
        return "b"

    async def b_undo(ctx: StepContext) -> None:
        rec.add("undo:b-attempt")
        raise RuntimeError("undo b blew up")  # compensation itself fails

    async def c_do(ctx: StepContext) -> None:
        raise TransientStepError("c failed")

    wf = Workflow(
        name="bestcomp",
        steps=(
            Step("a", a_do, compensation=a_undo, retry=NO_RETRY),
            Step("b", b_do, compensation=b_undo, retry=NO_RETRY),
            Step("c", c_do, retry=NO_RETRY),
        ),
    )
    engine, store, bus = _engine(wf)
    with pytest.raises(SagaFailed) as ei:
        await engine.start("bestcomp", run_id="C2")

    # b's compensation failed but the unwind CONTINUED to a (best-effort).
    assert rec.ops() == ["do:a", "do:b", "undo:b-attempt", "undo:a"]
    final = await store.load("C2")
    assert record_of(final, "b").compensation == CompensationOutcome.FAILED
    assert record_of(final, "b").compensation_error is not None
    assert record_of(final, "a").compensation == CompensationOutcome.OK
    assert final.compensation_failures == ["b"]
    assert final.compensated == ["a"]
    assert ei.value.compensation_failures == ["b"]
    assert bus.steps_of(SagaEventType.COMPENSATION_FAILED) == ["b"]


async def test_step_without_compensation_is_skipped_during_unwind() -> None:
    rec = Recorder()

    async def a_do(ctx: StepContext) -> str:
        rec.add("do:a")
        return "a"

    async def b_do(ctx: StepContext) -> str:  # no compensation
        rec.add("do:b")
        return "b"

    async def c_do(ctx: StepContext) -> None:
        rec.add("do:c")
        raise TransientStepError("c failed")

    async def a_undo(ctx: StepContext) -> None:
        rec.add("undo:a")

    wf = Workflow(
        name="partial",
        steps=(
            Step("a", a_do, compensation=a_undo, retry=NO_RETRY),
            Step("b", b_do, retry=NO_RETRY),  # uncompensated
            Step("c", c_do, retry=NO_RETRY),
        ),
    )
    engine, store, _ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("partial", run_id="C3")
    # only a has a compensation → only undo:a runs.
    assert rec.ops() == ["do:a", "do:b", "do:c", "undo:a"]
    final = await store.load("C3")
    assert final.compensated == ["a"]
    assert record_of(final, "b").compensation == CompensationOutcome.NONE


async def test_cancel_compensates_then_marks_cancelled() -> None:
    rec = Recorder()
    a_do, a_undo = _make_step(rec, "a")

    async def park(ctx: StepContext) -> None:
        return None  # never reached; we cancel after a

    wf = Workflow(
        name="cancelme",
        steps=(
            Step("a", a_do, compensation=a_undo, retry=NO_RETRY),
            Step("b", park, await_signal="go"),
        ),
    )
    engine, store, bus = _engine(wf)
    # start parks on b's await; a is completed and compensable.
    state = await engine.start("cancelme", run_id="C4")
    assert state.status == RunStatus.WAITING

    cancelled = await engine.cancel("C4", reason="operator stop")
    assert cancelled.status == RunStatus.CANCELLED
    assert rec.ops() == ["do:a", "undo:a"]  # a undone on cancel
    assert cancelled.compensated == ["a"]
    assert bus.types()[-1] == SagaEventType.RUN_CANCELLED
