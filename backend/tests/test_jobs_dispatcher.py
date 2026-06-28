"""Unit tests for the dispatcher's at-least-once / retry / DLQ semantics (no infra)."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from app.jobs.backoff import BackoffPolicy
from app.jobs.clock import ManualClock
from app.jobs.dispatcher import Dispatcher
from app.jobs.registry import JobRegistry, job
from app.jobs.store import InMemoryJobStore
from app.jobs.triggers import every
from app.jobs.types import JobContext, JobResult, JobRun, JobRunStatus, RunOutcome, TriggerKind


def at(mi: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, mi, s, tzinfo=UTC)


async def _setup(
    reg: JobRegistry, *, resources: dict[str, object] | None = None
) -> tuple[ManualClock, InMemoryJobStore, Dispatcher]:
    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    disp = Dispatcher(
        registry=reg,
        store=store,
        clock=clock,
        resources=resources or {},
        rng=random.Random(0),  # deterministic jitter
    )
    return clock, store, disp


async def _enqueue_and_claim(
    store: InMemoryJobStore, name: str, key: str, max_attempts: int
) -> JobRun | None:
    await store.enqueue(
        job_name=name,
        idempotency_key=key,
        scheduled_for=at(0),
        max_attempts=max_attempts,
        trigger_kind=TriggerKind.INTERVAL,
    )
    return await store.claim_due(now=at(0), lease_seconds=60)


async def test_successful_handler_completes_run() -> None:
    reg = JobRegistry()
    seen: list[int] = []

    @job("ok", trigger=every(60), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        seen.append(ctx.attempt)
        return JobResult.ok(rows=3)

    _clock, store, disp = await _setup(reg)
    run = await _enqueue_and_claim(store, "ok", "ok@k", 3)
    assert run is not None
    result = await disp.dispatch(run)
    assert result.decision == "completed"
    assert result.outcome is RunOutcome.SUCCESS
    stored = await store.get(run.id)
    assert stored is not None
    assert stored.status is JobRunStatus.SUCCEEDED
    assert stored.detail == {"rows": 3}
    assert seen == [1]


async def test_none_return_treated_as_success() -> None:
    reg = JobRegistry()

    @job("bare", trigger=every(60), registry=reg)
    async def handler(ctx: JobContext) -> None:
        return None

    _clock, store, disp = await _setup(reg)
    run = await _enqueue_and_claim(store, "bare", "bare@k", 3)
    assert run is not None
    result = await disp.dispatch(run)
    assert result.decision == "completed"


async def test_skipped_result_is_terminal_success() -> None:
    reg = JobRegistry()

    @job("skip", trigger=every(60), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.skipped("subsystem not wired")

    _clock, store, disp = await _setup(reg)
    run = await _enqueue_and_claim(store, "skip", "skip@k", 3)
    assert run is not None
    result = await disp.dispatch(run)
    assert result.outcome is RunOutcome.SKIPPED
    stored = await store.get(run.id)
    assert stored is not None
    assert stored.status is JobRunStatus.SKIPPED


async def test_raised_exception_retries_then_deadletters() -> None:
    reg = JobRegistry()
    calls: list[int] = []

    @job(
        "flaky",
        trigger=every(60),
        backoff=BackoffPolicy(max_attempts=3, base_delay_s=2.0, factor=4.0, jitter=False),
        registry=reg,
    )
    async def handler(ctx: JobContext) -> JobResult:
        calls.append(ctx.attempt)
        raise RuntimeError("nope")

    clock, store, disp = await _setup(reg)
    await store.enqueue(
        job_name="flaky",
        idempotency_key="flaky@k",
        scheduled_for=at(0),
        max_attempts=3,
        trigger_kind=TriggerKind.INTERVAL,
    )

    # Attempt 1 -> retry with 2s backoff.
    run1 = await store.claim_due(now=at(0), lease_seconds=60)
    assert run1 is not None
    r1 = await disp.dispatch(run1)
    assert r1.decision == "retry"
    assert r1.delay_s == 2.0
    stored = await store.get(run1.id)
    assert stored is not None
    assert stored.status is JobRunStatus.RETRYING
    assert stored.available_at == at(0, 2)

    # Attempt 2 -> retry with 8s backoff (2 * 4^1).
    run2 = await store.claim_due(now=at(0, 2), lease_seconds=60)
    assert run2 is not None
    assert run2.attempt == 2
    r2 = await disp.dispatch(run2)
    assert r2.decision == "retry"
    assert r2.delay_s == 8.0

    # Attempt 3 -> dead-letter (cap reached).
    run3 = await store.claim_due(now=at(0, 10), lease_seconds=60)
    assert run3 is not None
    assert run3.attempt == 3
    r3 = await disp.dispatch(run3)
    assert r3.decision == "deadletter"
    stored3 = await store.get(run3.id)
    assert stored3 is not None
    assert stored3.status is JobRunStatus.DEADLETTER
    assert "nope" in (stored3.error or "")
    assert calls == [1, 2, 3]


async def test_failed_result_follows_retry_path() -> None:
    reg = JobRegistry()

    @job("reportfail", trigger=every(60), max_attempts=1, registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.failed("explicit failure")

    _clock, store, disp = await _setup(reg)
    run = await _enqueue_and_claim(store, "reportfail", "rf@k", 1)
    assert run is not None
    result = await disp.dispatch(run)
    # max_attempts=1 -> first failure dead-letters.
    assert result.decision == "deadletter"
    stored = await store.get(run.id)
    assert stored is not None
    assert "explicit failure" in (stored.error or "")


async def test_unregistered_job_deadletters() -> None:
    reg = JobRegistry()  # empty
    clock = ManualClock(start=at(0))
    store = InMemoryJobStore(clock=clock)
    disp = Dispatcher(registry=reg, store=store, clock=clock)
    await store.enqueue(
        job_name="ghost",
        idempotency_key="ghost@k",
        scheduled_for=at(0),
        max_attempts=3,
        trigger_kind=TriggerKind.INTERVAL,
    )
    run = await store.claim_due(now=at(0), lease_seconds=60)
    assert run is not None
    result = await disp.dispatch(run)
    assert result.decision == "deadletter"
    stored = await store.get(run.id)
    assert stored is not None
    assert stored.status is JobRunStatus.DEADLETTER
    assert "no handler" in (stored.error or "")


async def test_handler_receives_injected_resources() -> None:
    reg = JobRegistry()
    captured: dict = {}

    @job("usesres", trigger=every(60), registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        captured["db"] = ctx.resource("db")
        captured["missing"] = ctx.resource("absent", "fallback")
        return JobResult.ok()

    _clock, store, disp = await _setup(reg, resources={"db": "the-session-factory"})
    run = await _enqueue_and_claim(store, "usesres", "ur@k", 3)
    assert run is not None
    await disp.dispatch(run)
    assert captured["db"] == "the-session-factory"
    assert captured["missing"] == "fallback"
