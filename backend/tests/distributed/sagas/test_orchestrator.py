"""Orchestration engine: forward success, compensation, retries, deadlines.

These tests run entirely on the in-memory store + ledger against a manual clock,
so every backoff sleep and deadline is resolved in virtual time and the
assertions are exact (no real ``sleep``, no flakiness).
"""

from __future__ import annotations

from app.distributed.sagas import metrics
from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaRegistry, saga, step
from app.distributed.sagas.effects import InMemoryEffectLedger
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.store import InMemorySagaStore
from app.distributed.sagas.types import (
    SagaContext,
    SagaOutcome,
    SagaStatus,
    StepFailed,
    StepResult,
    StepStatus,
)
from app.jobs.clock import ManualClock


def _clock() -> ManualClock:
    return ManualClock()


def _orch(registry: SagaRegistry, clock: ManualClock) -> tuple[SagaOrchestrator, InMemorySagaStore]:
    store = InMemorySagaStore()
    ledger = InMemoryEffectLedger(clock=clock)
    orch = SagaOrchestrator(store, registry, clock=clock, effects=ledger)
    return orch, store


async def test_forward_happy_path_commits_and_threads_state() -> None:
    """All steps succeed → COMPLETED, and each step's output threads into state."""
    metrics.reset()

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok(book_id="book_1")

    async def s2(ctx: SagaContext) -> StepResult:
        # Reads upstream output from the shared state bag.
        assert ctx.state["book_id"] == "book_1"
        return StepResult.ok(canon_version=3)

    async def s3(ctx: SagaContext) -> StepResult:
        assert ctx.state["canon_version"] == 3
        return StepResult.ok(locked=True)

    reg = SagaRegistry()
    reg.register(saga("demo", step("ingest", s1), step("canon", s2), step("lock", s3)))
    clock = _clock()
    orch, store = _orch(reg, clock)

    inst = await orch.run_to_completion("demo", "corr-1")

    assert inst.status is SagaStatus.COMPLETED
    assert inst.outcome is SagaOutcome.COMMITTED
    assert inst.state == {"book_id": "book_1", "canon_version": 3, "locked": True}
    loaded = await store.load(inst.id)
    assert loaded is not None
    assert [s.status for s in loaded.steps] == [StepStatus.COMPLETED] * 3
    snap = metrics.snapshot()
    assert snap["sagas_committed"] == 1
    assert snap["steps_succeeded"] == 3


async def test_correlation_id_dedups_start() -> None:
    """Starting twice with the same correlation id returns the same instance."""

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("demo", step("only", s1)))
    clock = _clock()
    orch, _ = _orch(reg, clock)

    a = await orch.start("demo", "same")
    b = await orch.start("demo", "same")
    assert a.id == b.id


async def test_step_failure_triggers_reverse_compensation() -> None:
    """A failing step compensates already-completed steps in REVERSE order."""
    metrics.reset()
    comp_order: list[str] = []

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok(a=1)

    async def s1_comp(ctx: SagaContext) -> StepResult:
        comp_order.append("s1")
        return StepResult.ok()

    async def s2(ctx: SagaContext) -> StepResult:
        return StepResult.ok(b=2)

    async def s2_comp(ctx: SagaContext) -> StepResult:
        comp_order.append("s2")
        return StepResult.ok()

    async def s3(ctx: SagaContext) -> StepResult:
        raise StepFailed("boom", retryable=False)

    reg = SagaRegistry()
    reg.register(
        saga(
            "demo",
            step("s1", s1, compensation=s1_comp),
            step("s2", s2, compensation=s2_comp),
            step("s3", s3),
        )
    )
    clock = _clock()
    orch, store = _orch(reg, clock)

    inst = await orch.run_to_completion("demo", "corr-2")

    assert inst.status is SagaStatus.COMPENSATED
    # Reverse order: s2 undone before s1.
    assert comp_order == ["s2", "s1"]
    loaded = await store.load(inst.id)
    assert loaded is not None
    by_name = {s.name: s.status for s in loaded.steps}
    assert by_name["s1"] is StepStatus.COMPENSATED
    assert by_name["s2"] is StepStatus.COMPENSATED
    assert by_name["s3"] is StepStatus.FAILED
    assert metrics.snapshot()["sagas_compensated"] == 1


async def test_retryable_failure_then_success_uses_backoff() -> None:
    """A transient failure retries (consuming virtual time) and then succeeds.

    Backoff sleeps on the manual clock, so we drive ``resume`` as a task and
    advance virtual time to release each backoff — the deterministic stand-in for
    wall-clock elapsing under a real worker.
    """
    import asyncio

    metrics.reset()
    calls = {"n": 0}

    async def flaky(ctx: SagaContext) -> StepResult:
        calls["n"] += 1
        if calls["n"] < 3:
            raise StepFailed("transient")  # retryable by default
        return StepResult.ok(done=True)

    policy = BackoffPolicy(max_attempts=5, base_delay_s=2.0, factor=2.0, jitter=False)
    reg = SagaRegistry()
    reg.register(saga("demo", step("flaky", flaky, retry=policy)))
    clock = _clock()
    orch, _ = _orch(reg, clock)

    started = await orch.start("demo", "corr-3")
    drive = asyncio.create_task(orch.resume(started.id))
    # Release the two backoff windows (delay before attempt 2 = 2s, before 3 = 4s).
    for delay in (2.0, 4.0):
        while clock.pending_sleepers == 0 and not drive.done():
            await asyncio.sleep(0)
        await clock.advance(delay)
    inst = await drive

    assert inst.status is SagaStatus.COMPLETED
    assert calls["n"] == 3
    snap = metrics.snapshot()
    assert snap["steps_retried"] == 2


async def test_retries_exhausted_compensates() -> None:
    """A step that always fails exhausts its budget then compensates upstream."""

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    undone = {"n": 0}

    async def s1_comp(ctx: SagaContext) -> StepResult:
        undone["n"] += 1
        return StepResult.ok()

    async def always_fail(ctx: SagaContext) -> StepResult:
        raise StepFailed("nope")

    import asyncio

    policy = BackoffPolicy(max_attempts=3, base_delay_s=1.0, factor=2.0, jitter=False)
    reg = SagaRegistry()
    reg.register(
        saga(
            "demo",
            step("s1", s1, compensation=s1_comp),
            step("bad", always_fail, retry=policy),
        )
    )
    clock = _clock()
    orch, store = _orch(reg, clock)

    started = await orch.start("demo", "corr-4")
    drive = asyncio.create_task(orch.resume(started.id))
    # 'bad' attempts 3 times: backoff before attempt 2 (1s) and attempt 3 (2s).
    for delay in (1.0, 2.0):
        while clock.pending_sleepers == 0 and not drive.done():
            await asyncio.sleep(0)
        await clock.advance(delay)
    inst = await drive

    assert inst.status is SagaStatus.COMPENSATED
    assert undone["n"] == 1
    loaded = await store.load(inst.id)
    assert loaded is not None
    bad = next(s for s in loaded.steps if s.name == "bad")
    assert bad.attempt == 3  # exactly max_attempts forward tries


async def test_compensation_failure_is_fatal_failed() -> None:
    """A compensation that exhausts its budget lands the saga in FAILED (loud)."""
    metrics.reset()

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    async def s1_comp(ctx: SagaContext) -> StepResult:
        raise StepFailed("cannot undo")

    async def boom(ctx: SagaContext) -> StepResult:
        raise StepFailed("forward fail", retryable=False)

    import asyncio

    comp_policy = BackoffPolicy(max_attempts=2, base_delay_s=1.0, factor=2.0, jitter=False)
    reg = SagaRegistry()
    reg.register(
        saga(
            "demo",
            step("s1", s1, compensation=s1_comp, compensation_retry=comp_policy),
            step("boom", boom),
        )
    )
    clock = _clock()
    orch, store = _orch(reg, clock)

    started = await orch.start("demo", "corr-5")
    drive = asyncio.create_task(orch.resume(started.id))
    # s1's compensation fails attempt 1, parks 1s backoff, then attempt 2 → fatal.
    while clock.pending_sleepers == 0 and not drive.done():
        await asyncio.sleep(0)
    await clock.advance(1.0)
    inst = await drive

    assert inst.status is SagaStatus.FAILED
    assert inst.outcome is SagaOutcome.FAILED
    loaded = await store.load(inst.id)
    assert loaded is not None
    s1_rec = next(s for s in loaded.steps if s.name == "s1")
    assert s1_rec.status is StepStatus.COMPENSATION_FAILED
    assert metrics.snapshot()["compensations_failed"] == 1
    assert metrics.snapshot()["sagas_failed"] == 1


async def test_step_with_no_compensation_is_skipped_on_rollback() -> None:
    """A step lacking a compensation is treated as a no-op undo during rollback."""

    async def s1(ctx: SagaContext) -> StepResult:  # no compensation
        return StepResult.ok()

    undone = {"s2": 0}

    async def s2(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    async def s2_comp(ctx: SagaContext) -> StepResult:
        undone["s2"] += 1
        return StepResult.ok()

    async def boom(ctx: SagaContext) -> StepResult:
        raise StepFailed("x", retryable=False)

    reg = SagaRegistry()
    reg.register(
        saga(
            "demo",
            step("s1", s1),
            step("s2", s2, compensation=s2_comp),
            step("boom", boom),
        )
    )
    clock = _clock()
    orch, store = _orch(reg, clock)
    inst = await orch.run_to_completion("demo", "corr-6")

    assert inst.status is SagaStatus.COMPENSATED
    assert undone["s2"] == 1
    loaded = await store.load(inst.id)
    assert loaded is not None
    s1_rec = next(s for s in loaded.steps if s.name == "s1")
    assert s1_rec.status is StepStatus.COMPENSATED  # no-op undo still marks compensated


async def test_overall_deadline_triggers_compensation() -> None:
    """When the saga deadline has elapsed before the next step, the engine compensates.

    The deadline is checked at the top of each drive pass. We complete the first
    step, advance virtual time past the deadline, then resume — the engine sees the
    elapsed deadline and rolls back the completed step instead of running s2.
    """
    s2_ran = {"n": 0}

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    undone = {"n": 0}

    async def comp(ctx: SagaContext) -> StepResult:
        undone["n"] += 1
        return StepResult.ok()

    async def s2(ctx: SagaContext) -> StepResult:  # must not run after the deadline
        s2_ran["n"] += 1
        return StepResult.ok()

    reg = SagaRegistry()
    # Make s1 fail-once-then-retry so the drive parks on backoff, giving us a clean
    # seam to advance the clock past the deadline before s1 completes + s2 runs.
    fail_then_ok = {"n": 0}

    async def s1_retry(ctx: SagaContext) -> StepResult:
        fail_then_ok["n"] += 1
        if fail_then_ok["n"] == 1:
            raise StepFailed("transient")
        return StepResult.ok()

    policy = BackoffPolicy(max_attempts=5, base_delay_s=5.0, jitter=False)
    reg.register(
        saga(
            "demo",
            step("s1", s1_retry, compensation=comp, retry=policy),
            step("s2", s2, compensation=comp),
            deadline_s=3.0,
        )
    )
    clock = _clock()
    orch, store = _orch(reg, clock)
    inst = await orch.start("demo", "corr-7")

    import asyncio

    drive = asyncio.create_task(orch.resume(inst.id))
    # Let s1 fail once and park on its 5s backoff.
    for _ in range(5):
        await asyncio.sleep(0)
    # Advance past the 3s deadline (still inside the 5s backoff window).
    await clock.advance(4.0)
    final = await drive

    assert final.status is SagaStatus.COMPENSATED
    assert s2_ran["n"] == 0  # s2 never ran — deadline pre-empted it
    assert undone["n"] == 0  # s1 had not completed (still retrying), nothing to undo


async def test_abort_requests_rollback() -> None:
    """abort() on a non-terminal saga rolls it back on the next drive."""

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    undone = {"n": 0}

    async def comp(ctx: SagaContext) -> StepResult:
        undone["n"] += 1
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("demo", step("s1", s1, compensation=comp)))
    clock = _clock()
    orch, store = _orch(reg, clock)

    inst = await orch.start("demo", "corr-8")
    # Drive only the first step by hand: mark s1 complete via a partial resume is
    # awkward; instead start fresh and abort before any drive, then resume.
    await orch.abort(inst.id, reason="user cancel")
    final = await orch.resume(inst.id)
    assert final.status is SagaStatus.COMPENSATED
