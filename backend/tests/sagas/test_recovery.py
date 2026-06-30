"""Recovery sweep: fire due timers + re-claim abandoned (expired-lease) runs."""

from __future__ import annotations

import pytest

from app.sagas import (
    FakeClock,
    InMemoryDurableStore,
    RecordingBus,
    RecoverySweeper,
    RunStatus,
    SagaEngine,
    SagaEventType,
    Step,
    StepContext,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from tests.sagas.helpers import AdvancingSleeper, Recorder, seq_run_ids


class _Crash(BaseException):
    pass


def _wire(
    wf: Workflow, *, lease_ttl_s: float = 100.0
) -> tuple[SagaEngine, InMemoryDurableStore, FakeClock, RecordingBus, RecoverySweeper]:
    clock = FakeClock()
    store = InMemoryDurableStore()
    bus = RecordingBus()
    engine = SagaEngine(
        WorkflowRegistry([wf]),
        store,
        clock=clock,
        sleeper=AdvancingSleeper(clock),
        bus=bus,
        run_id_factory=seq_run_ids(),
        lease_ttl_s=lease_ttl_s,
    )
    sweeper = RecoverySweeper(engine, store, clock=clock, bus=bus)
    return engine, store, clock, bus, sweeper


async def test_sweep_fires_due_timer_and_routes_await_timeout() -> None:
    rec = Recorder()

    async def gate(ctx: StepContext) -> str:
        rec.add("gate")  # never runs (signal never arrives)
        return "g"

    async def fallback(ctx: StepContext) -> str:
        rec.add("fallback")
        return "f"

    wf = Workflow(
        name="sweep_timer",
        steps=(
            Step(
                "gate",
                gate,
                await_signal="never",
                await_timeout_s=30.0,
                on_await_timeout="fallback",
            ),
            Step("fallback", fallback),
        ),
    )
    engine, store, clock, bus, sweeper = _wire(wf)
    state = await engine.start("sweep_timer", run_id="RC1")
    assert state.status == RunStatus.WAITING

    # Before the deadline: the sweep finds nothing due.
    clock.advance(10.0)
    report = await sweeper.sweep()
    assert report.total == 0
    assert (await store.load("RC1")).status == RunStatus.WAITING

    # After the deadline: the sweep fires the timer → run routes to fallback.
    clock.advance(25.0)  # t=35 > fire_at=30
    report = await sweeper.sweep()
    assert "RC1" in report.fired_timers
    final = await store.load("RC1")
    assert final.status == RunStatus.COMPLETED
    assert rec.ops() == ["fallback"]


async def test_sweep_recovers_stuck_run_with_expired_lease() -> None:
    rec = Recorder()
    crash_once = {"armed": True}

    async def a(ctx: StepContext) -> str:
        rec.add("a")
        return "a"

    async def b(ctx: StepContext) -> str:
        if crash_once["armed"]:
            crash_once["armed"] = False
            raise _Crash("worker died mid-b")
        rec.add("b")
        return "b"

    async def c(ctx: StepContext) -> str:
        rec.add("c")
        return "c"

    wf = Workflow(name="stuck", steps=(Step("a", a), Step("b", b), Step("c", c)))
    engine, store, clock, bus, sweeper = _wire(wf, lease_ttl_s=100.0)

    # The worker crashes mid-b, leaving a RUNNING run holding a lease.
    with pytest.raises(_Crash):
        await engine.start("stuck", run_id="RC2")
    stuck = await store.load("RC2")
    assert stuck.status == RunStatus.RUNNING
    assert stuck.lease_until is not None

    # Before the lease expires: the sweep won't steal a live run.
    clock.advance(50.0)
    report = await sweeper.sweep()
    assert report.recovered_stuck == []

    # After the lease expires: the sweep re-claims and resumes from b.
    clock.advance(60.0)  # t=110 > lease_until=100
    report = await sweeper.sweep()
    assert "RC2" in report.recovered_stuck
    final = await store.load("RC2")
    assert final.status == RunStatus.COMPLETED
    assert rec.ops() == ["a", "b", "c"]  # a not re-run; b retried after recovery
    assert rec.count("a") == 1
    assert SagaEventType.RUN_RECOVERED in bus.types()


async def test_sweep_is_idempotent_when_nothing_to_do() -> None:
    async def s(ctx: StepContext) -> None:
        return None

    wf = Workflow(name="quiet", steps=(Step("s", s),))
    engine, store, clock, bus, sweeper = _wire(wf)
    await engine.start("quiet", run_id="RC3")
    report = await sweeper.sweep()
    assert report.total == 0 and report.failed == []


async def test_engine_fire_due_timers_directly() -> None:
    """The engine exposes fire_due_timers (the sweep's timer pass) directly."""
    rec = Recorder()

    async def gate(ctx: StepContext) -> str:
        rec.add("gate")
        return "g"

    async def fb(ctx: StepContext) -> str:
        rec.add("fb")
        return "f"

    wf = Workflow(
        name="direct",
        steps=(
            Step("gate", gate, await_signal="x", await_timeout_s=5.0, on_await_timeout="fb"),
            Step("fb", fb),
        ),
    )
    engine, store, clock, bus, _ = _wire(wf)
    await engine.start("direct", run_id="RC4")
    clock.advance(6.0)
    resumed = await engine.fire_due_timers()
    assert len(resumed) == 1
    assert resumed[0].status == RunStatus.COMPLETED
    assert rec.ops() == ["fb"]
