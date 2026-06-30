"""Crash-resume + deterministic replay idempotency.

These are the engine's core guarantees: a crash mid-flow resumes from the last
*completed* step (not the top), and re-driving a run never re-executes a
completed step's side effect (the recorded result is replayed) — same history,
same path, no double effects.
"""

from __future__ import annotations

import pytest

from app.sagas import (
    Action,
    FakeClock,
    InMemoryDurableStore,
    PermanentStepError,
    RecordingBus,
    RunState,
    RunStatus,
    SagaEngine,
    SagaEventType,
    Step,
    StepContext,
    StepStatus,
    TransientStepError,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from tests.sagas.helpers import AdvancingSleeper, Recorder, record_of, seq_run_ids


class _Crash(BaseException):
    """A simulated process crash that bypasses the engine's error handling.

    Subclasses :class:`BaseException` so it is *not* caught by the engine's
    ``except StepError`` / ``except Exception`` paths — it propagates like a hard
    kill, leaving the store at the last persisted state.
    """


def _new_engine(
    store: InMemoryDurableStore, wf: Workflow, clock: FakeClock, bus: RecordingBus
) -> SagaEngine:
    return SagaEngine(
        WorkflowRegistry([wf]),
        store,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus,
        run_id_factory=seq_run_ids(),
    )


async def test_crash_mid_flow_resumes_from_last_completed_step() -> None:
    rec = Recorder()
    crash_on = {"b"}

    def make(name: str) -> Action:
        async def action(ctx: StepContext) -> str:
            if name in crash_on:
                crash_on.discard(name)  # crash only once
                raise _Crash(f"killed at {name}")
            rec.add(name)
            return f"{name}-v"

        return action

    wf = Workflow(name="resumable", steps=tuple(Step(n, make(n)) for n in ("a", "b", "c", "d")))
    store = InMemoryDurableStore()
    clock = FakeClock()

    # First engine: a completes, b "crashes" (process dies).
    e1 = _new_engine(store, wf, clock, RecordingBus())
    with pytest.raises(_Crash):
        await e1.start("resumable", {"k": 1}, run_id="R1")

    mid = await store.load("R1")
    assert record_of(mid, "a").status == StepStatus.COMPLETED
    assert record_of(mid, "b").status in (StepStatus.PENDING, StepStatus.RUNNING)
    assert rec.ops() == ["a"]  # only a ran its body

    # Second (fresh) engine resumes the persisted run — a is NOT re-run.
    bus2 = RecordingBus()
    e2 = _new_engine(store, wf, clock, bus2)
    final = await e2.resume("R1")

    assert final.status == RunStatus.COMPLETED
    # a already done → not re-run (cursor resumed past it); b, c, d ran once.
    assert rec.ops() == ["a", "b", "c", "d"]
    assert rec.count("a") == 1
    # resume picked up from the persisted cursor, not the top.
    assert bus2.types()[0] == SagaEventType.RUN_RESUMED
    assert bus2.steps_of(SagaEventType.STEP_COMPLETED) == ["b", "c", "d"]
    # a's recorded result is preserved verbatim across the resume.
    assert record_of(final, "a").result == "a-v"


async def test_replay_skips_completed_step_when_crash_lands_before_cursor_advance() -> None:
    """A crash *between* a step completing and the cursor advancing is the case
    that needs the replay safety-net: on resume the just-completed step is
    re-entered, recognised COMPLETED, emits ``step_skipped`` and is not re-run."""
    rec = Recorder()

    async def a(ctx: StepContext) -> str:
        rec.add("a")
        return "a-done"

    async def b(ctx: StepContext) -> str:
        rec.add("b")
        return "b-done"

    wf = Workflow(name="window", steps=(Step("a", a), Step("b", b)))
    clock = FakeClock()

    # A store that crashes once, on the save that records cursor==1 after a.
    class _CrashingStore(InMemoryDurableStore):
        armed = True

        async def save(self, state: RunState, *, expected_revision: int) -> RunState:
            a_rec = state.step_by_name("a")
            if self.armed and state.cursor == 1 and a_rec is not None and a_rec.is_done:
                self.armed = False
                raise _Crash("crash on cursor-advance persist")
            return await super().save(state, expected_revision=expected_revision)

    crashing = _CrashingStore()
    e1 = SagaEngine(
        WorkflowRegistry([wf]),
        crashing,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=RecordingBus(),
        run_id_factory=seq_run_ids(),
    )
    with pytest.raises(_Crash):
        await e1.start("window", run_id="RW")

    persisted = await crashing.load("RW")
    assert record_of(persisted, "a").is_done
    assert persisted.cursor == 0  # the advance never persisted

    bus2 = RecordingBus()
    e2 = SagaEngine(
        WorkflowRegistry([wf]),
        crashing,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus2,
        run_id_factory=seq_run_ids(),
    )
    final = await e2.resume("RW")
    assert final.status == RunStatus.COMPLETED
    assert rec.count("a") == 1  # a NOT re-executed despite cursor==0
    assert "a" in bus2.steps_of(SagaEventType.STEP_SKIPPED)


async def test_replay_does_not_repeat_side_effects_and_returns_recorded_result() -> None:
    rec = Recorder()

    async def charge(ctx: StepContext) -> str:
        rec.add("charge", ctx.idempotency_key)
        return "reservation-1"

    async def use(ctx: StepContext) -> str:
        # reads the recorded result of the prior step
        assert ctx.result_of("charge") == "reservation-1"
        rec.add("use")
        return "used"

    wf = Workflow(name="charge", steps=(Step("charge", charge), Step("use", use)))
    store = InMemoryDurableStore()
    clock = FakeClock()
    e = _new_engine(store, wf, clock, RecordingBus())
    state = await e.start("charge", run_id="R2")
    assert state.status == RunStatus.COMPLETED

    # Re-drive the *already completed* run: a terminal run is a no-op, no charge.
    again = await e.resume("R2")
    assert again.status == RunStatus.COMPLETED
    assert rec.count("charge") == 1
    assert rec.count("use") == 1


async def test_idempotency_key_is_stable_across_resume() -> None:
    """The same logical step yields the same idempotency key before/after a crash."""
    seen_keys: list[str] = []

    async def step(ctx: StepContext) -> str:
        seen_keys.append(ctx.idempotency_key)
        if len(seen_keys) == 1:
            raise TransientStepError("transient: try again on resume")
        return "ok"

    wf = Workflow(name="key", steps=(Step("s", step),))
    store = InMemoryDurableStore()
    clock = FakeClock()
    # max_attempts default 3 → retried in-process; both attempts share the key.
    e = _new_engine(store, wf, clock, RecordingBus())
    state = await e.start("key", {"book_id": "b"}, run_id="R3")
    assert state.status == RunStatus.COMPLETED
    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]  # attempt-invariant


async def test_resume_of_terminal_run_is_noop() -> None:
    async def s(ctx: StepContext) -> None:
        return None

    wf = Workflow(name="t", steps=(Step("s", s),))
    store = InMemoryDurableStore()
    clock = FakeClock()
    e = _new_engine(store, wf, clock, RecordingBus())
    st = await e.start("t", run_id="R4")
    assert st.status == RunStatus.COMPLETED
    again = await e.resume("R4")
    assert again.revision == st.revision  # no further writes


async def test_permanent_error_is_not_retried() -> None:
    attempts = {"n": 0}

    async def boom(ctx: StepContext) -> None:
        attempts["n"] += 1
        raise PermanentStepError("do not retry me")

    wf = Workflow(name="perm", steps=(Step("boom", boom),))
    store = InMemoryDurableStore()
    clock = FakeClock()
    from app.sagas import SagaFailed

    e = _new_engine(store, wf, clock, RecordingBus())
    with pytest.raises(SagaFailed):
        await e.start("perm", run_id="R5")
    assert attempts["n"] == 1  # permanent → single attempt
    final = await store.load("R5")
    assert final.status == RunStatus.FAILED
