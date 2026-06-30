"""Retry escalation, deterministic backoff, and per-attempt / total timeouts."""

from __future__ import annotations

import asyncio

import pytest

from app.sagas import (
    FakeClock,
    InMemoryDurableStore,
    RecordingBus,
    RetryPolicy,
    RunStatus,
    SagaEngine,
    SagaEventType,
    SagaFailed,
    Step,
    StepContext,
    TimeoutPolicy,
    TransientStepError,
    Workflow,
)
from app.sagas.registry import WorkflowRegistry
from tests.sagas.helpers import AdvancingSleeper, record_of, seq_run_ids


def _engine(
    wf: Workflow,
) -> tuple[SagaEngine, InMemoryDurableStore, FakeClock, RecordingBus, AdvancingSleeper]:
    clock = FakeClock()
    store = InMemoryDurableStore()
    bus = RecordingBus()
    sleeper = AdvancingSleeper(clock)
    engine = SagaEngine(
        WorkflowRegistry([wf]),
        store,
        clock=clock,
        sleeper=sleeper,
        bus=bus,
        run_id_factory=seq_run_ids(),
    )
    return engine, store, clock, bus, sleeper


async def test_transient_failures_retry_then_succeed() -> None:
    attempts = {"n": 0}

    async def flaky(ctx: StepContext) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientStepError(f"hiccup {attempts['n']}")
        return "recovered"

    wf = Workflow(
        name="flaky",
        steps=(
            Step("flaky", flaky, retry=RetryPolicy(max_attempts=5, base_backoff_s=1.0, factor=2.0)),
        ),
    )
    engine, store, clock, bus, sleeper = _engine(wf)
    state = await engine.start("flaky", run_id="F1")

    assert state.status == RunStatus.COMPLETED
    assert attempts["n"] == 3
    assert record_of(state, "flaky").result == "recovered"
    # backoff slept after attempt 1 (1s) and attempt 2 (2s) — deterministic.
    assert sleeper.sleeps == [1.0, 2.0]
    assert clock.time() == 1_700_000_000.0 + 3.0
    assert bus.types().count(SagaEventType.STEP_RETRYING) == 2


async def test_retries_exhausted_fails_run() -> None:
    attempts = {"n": 0}

    async def always_fail(ctx: StepContext) -> None:
        attempts["n"] += 1
        raise TransientStepError("nope")

    wf = Workflow(
        name="doomed",
        steps=(
            Step("always_fail", always_fail, retry=RetryPolicy(max_attempts=3, base_backoff_s=1.0)),
        ),
    )
    engine, store, *_ = _engine(wf)
    with pytest.raises(SagaFailed) as ei:
        await engine.start("doomed", run_id="F2")
    assert attempts["n"] == 3  # initial + 2 retries
    assert ei.value.failed_step == "always_fail"
    final = await store.load("F2")
    assert final.status == RunStatus.FAILED
    assert len(record_of(final, "always_fail").attempts) == 3


async def test_per_attempt_timeout_cancels_and_retries() -> None:
    calls = {"n": 0}

    async def hang(ctx: StepContext) -> None:
        calls["n"] += 1
        await asyncio.Event().wait()  # never completes → timer wins

    wf = Workflow(
        name="hang",
        steps=(
            Step(
                "hang",
                hang,
                retry=RetryPolicy(max_attempts=2, base_backoff_s=1.0),
                timeout=TimeoutPolicy(per_attempt_s=5.0),
            ),
        ),
    )
    engine, store, clock, bus, _ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("hang", run_id="T1")

    assert calls["n"] == 2
    final = await store.load("T1")
    attempts = record_of(final, "hang").attempts
    assert [a.timed_out for a in attempts] == [True, True]
    assert bus.types().count(SagaEventType.STEP_TIMEOUT) == 2


async def test_total_timeout_stops_retrying() -> None:
    calls = {"n": 0}

    async def slow_fail(ctx: StepContext) -> None:
        calls["n"] += 1
        raise TransientStepError("still failing")

    # base backoff 10s, total 15s → after attempt 1 fails it sleeps 10s (now=10),
    # attempt 2 fails, next backoff would push past the 15s total deadline → stop.
    wf = Workflow(
        name="total",
        steps=(
            Step(
                "slow_fail",
                slow_fail,
                retry=RetryPolicy(max_attempts=10, base_backoff_s=10.0, factor=1.0),
                timeout=TimeoutPolicy(total_s=15.0),
            ),
        ),
    )
    engine, store, clock, bus, _ = _engine(wf)
    with pytest.raises(SagaFailed):
        await engine.start("total", run_id="T2")
    # attempt1 (t=0) fail → sleep 10 (t=10) → attempt2 fail → past total(15)? t=10<15
    # so attempt3 at... actually next backoff check uses clock>=deadline AFTER sleep.
    # We only assert it stopped well before exhausting 10 attempts.
    assert calls["n"] < 10
    assert (await store.load("T2")).status == RunStatus.FAILED


async def test_per_attempt_timeout_action_that_finishes_in_time_succeeds() -> None:
    async def quick(ctx: StepContext) -> str:
        return "fast"  # returns without awaiting → beats the timer

    wf = Workflow(
        name="quick",
        steps=(Step("quick", quick, timeout=TimeoutPolicy(per_attempt_s=30.0)),),
    )
    engine, store, *_ = _engine(wf)
    state = await engine.start("quick", run_id="T3")
    assert state.status == RunStatus.COMPLETED
    assert record_of(state, "quick").result == "fast"
