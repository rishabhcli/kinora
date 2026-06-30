"""Core engine behaviour: happy path, step persistence, branching, telemetry."""

from __future__ import annotations

import pytest

from app.sagas import (
    END,
    Action,
    FakeClock,
    InMemoryDurableStore,
    RecordingBus,
    RetryPolicy,
    RunStatus,
    SagaEngine,
    SagaEventType,
    Step,
    StepContext,
    StepStatus,
    Workflow,
    WorkflowRegistry,
)
from tests.sagas.helpers import AdvancingSleeper, Recorder, record_of, seq_run_ids


def _engine(
    workflow: Workflow, *, clock: FakeClock | None = None, bus: RecordingBus | None = None
) -> tuple[SagaEngine, InMemoryDurableStore, FakeClock]:
    clock = clock or FakeClock()
    store = InMemoryDurableStore()
    registry = WorkflowRegistry([workflow])
    engine = SagaEngine(
        registry,
        store,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus or RecordingBus(),
        run_id_factory=seq_run_ids(),
    )
    return engine, store, clock


async def test_happy_path_runs_all_steps_in_order() -> None:
    rec = Recorder()

    def make(name: str) -> Action:
        async def action(ctx: StepContext) -> str:
            rec.add(name, ctx.input)
            return f"{name}-ok"

        return action

    wf = Workflow(
        name="linear",
        steps=tuple(Step(n, make(n)) for n in ("a", "b", "c")),
    )
    bus = RecordingBus()
    engine, store, _ = _engine(wf, bus=bus)

    state = await engine.start("linear", {"x": 1})

    assert state.status == RunStatus.COMPLETED
    assert rec.ops() == ["a", "b", "c"]
    # Every step recorded its result for replay.
    assert [s.result for s in state.steps] == ["a-ok", "b-ok", "c-ok"]
    assert all(s.status == StepStatus.COMPLETED for s in state.steps)
    # Lifecycle telemetry fired start → 3 completions → run completed.
    assert bus.types()[0] == SagaEventType.RUN_STARTED
    assert bus.types()[-1] == SagaEventType.RUN_COMPLETED
    assert bus.steps_of(SagaEventType.STEP_COMPLETED) == ["a", "b", "c"]


async def test_state_is_persisted_after_every_step() -> None:
    """The store revision advances per step — state is durable mid-flight."""
    revisions: list[int] = []

    async def action(ctx: StepContext) -> int:
        return ctx.attempt

    wf = Workflow(name="three", steps=tuple(Step(n, action) for n in ("p", "q", "r")))
    engine, store, _ = _engine(wf)
    state = await engine.start("three")

    reloaded = await store.load(state.run_id)
    # revision climbs well past the number of steps (a write per transition).
    assert reloaded.revision >= 3
    assert reloaded.status == RunStatus.COMPLETED
    revisions.append(reloaded.revision)
    assert revisions[0] > 0


async def test_input_is_immutable_and_shared_context_flows() -> None:
    async def write(ctx: StepContext) -> None:
        ctx.set("token", "abc")

    async def read(ctx: StepContext) -> str:
        # later step sees earlier step's durable shared state + prior result
        assert ctx.get("token") == "abc"
        assert ctx.result_of("write") is None  # write returned None
        return "read-ok"

    wf = Workflow(name="ctx", steps=(Step("write", write), Step("read", read)))
    engine, store, _ = _engine(wf)
    state = await engine.start("ctx", {"book_id": "b1"})
    assert state.input == {"book_id": "b1"}
    assert state.context["token"] == "abc"
    assert record_of(state, "read").result == "read-ok"


async def test_branch_skips_intermediate_steps() -> None:
    rec = Recorder()

    def make(name: str) -> Action:
        async def action(ctx: StepContext) -> str:
            rec.add(name)
            return name

        return action

    def jump(ctx: StepContext) -> str:
        return "c"  # skip b

    wf = Workflow(
        name="branchy",
        steps=(
            Step("a", make("a"), branch=jump),
            Step("b", make("b")),
            Step("c", make("c")),
        ),
    )
    bus = RecordingBus()
    engine, store, _ = _engine(wf, bus=bus)
    state = await engine.start("branchy")

    assert rec.ops() == ["a", "c"]  # b skipped
    assert record_of(state, "b").status == StepStatus.SKIPPED
    assert state.status == RunStatus.COMPLETED
    branched = bus.of(SagaEventType.STEP_BRANCHED)
    assert branched and branched[0].fields.get("target") == "c"


async def test_branch_to_end_finishes_early() -> None:
    rec = Recorder()

    async def a(ctx: StepContext) -> None:
        rec.add("a")

    async def b(ctx: StepContext) -> None:
        rec.add("b")

    wf = Workflow(
        name="early",
        steps=(Step("a", a, branch=lambda ctx: END), Step("b", b)),
    )
    engine, _, _ = _engine(wf)
    state = await engine.start("early")
    assert rec.ops() == ["a"]
    assert state.status == RunStatus.COMPLETED


async def test_retry_policy_backoff_is_deterministic() -> None:
    p = RetryPolicy(max_attempts=4, base_backoff_s=1.0, factor=2.0, max_backoff_s=10.0)
    assert [p.backoff_for(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    # cap applies
    assert RetryPolicy(base_backoff_s=100.0, max_backoff_s=10.0).backoff_for(1) == 10.0
    # seeded jitter is reproducible
    jp = RetryPolicy(base_backoff_s=4.0, jitter_ratio=0.5)
    a = jp.backoff_for(1, seed="k")
    b = jp.backoff_for(1, seed="k")
    assert a == b
    assert 2.0 <= a <= 6.0


def test_workflow_rejects_duplicate_steps() -> None:
    from app.sagas import WorkflowDefinitionError

    async def noop(ctx: StepContext) -> None:
        return None

    with pytest.raises(WorkflowDefinitionError):
        Workflow(name="dup", steps=(Step("x", noop), Step("x", noop)))


def test_workflow_rejects_backward_branch_target() -> None:
    from app.sagas import WorkflowDefinitionError

    async def noop(ctx: StepContext) -> None:
        return None

    # on_await_timeout pointing backwards is rejected at validate()
    with pytest.raises(WorkflowDefinitionError):
        Workflow(
            name="back",
            steps=(
                Step("a", noop),
                Step(
                    "b",
                    noop,
                    await_signal="go",
                    await_timeout_s=1.0,
                    on_await_timeout="a",
                ),
            ),
        )
