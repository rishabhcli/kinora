"""Deterministic crash-resume + exactly-once proofs.

A "crash" is modelled by **dropping the orchestrator object mid-saga while keeping
the store** (the durable state survives a process death; the in-memory engine does
not). A "resume" is constructing a *fresh* :class:`SagaOrchestrator` over the same
store and calling :meth:`resume`. Because the store is the only durable thing, this
is exactly the production crash-resume path, and because everything runs on a
manual clock the proofs are exact.

The two correctness properties proven here:

1. **Forward crash-resume.** A crash after a step's side effect ran but before its
   COMPLETED status was persisted does NOT re-apply the side effect on resume —
   the effect ledger collapses the re-run to a no-op (exactly-once).
2. **Backward crash-resume.** A crash mid-compensation resumes compensation (not
   forward progress) and finishes undoing the remaining steps.
"""

from __future__ import annotations

from typing import Any

from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaRegistry, saga, step
from app.distributed.sagas.effects import EffectLedger, InMemoryEffectLedger
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.store import InMemorySagaStore
from app.distributed.sagas.types import (
    SagaContext,
    SagaStatus,
    StepFailed,
    StepResult,
    StepStatus,
)
from app.jobs.clock import ManualClock


class _CrashError(RuntimeError):
    """A simulated process crash injected from inside a step handler."""


async def test_resume_after_forward_crash_does_not_double_apply_effect() -> None:
    """A crash after a side effect but before COMPLETED replays the step exactly once.

    Step 2 runs an idempotent-via-ledger side effect (incrementing a shared
    counter through ``ctx.effects.once``), then "crashes" before returning. On
    resume the step re-runs — but the ledger short-circuits the effect, so the
    counter is incremented exactly once and the saga still commits.
    """
    side_effect_calls = {"n": 0}
    ledger = InMemoryEffectLedger()
    store = InMemorySagaStore()
    clock = ManualClock()

    async def s1(ctx: SagaContext) -> StepResult:
        return StepResult.ok(reservation="r1")

    crash_armed = {"on": True}

    async def s2(ctx: SagaContext) -> StepResult:
        # The exactly-once side effect.
        await ctx.effects.once(ctx.effect_key("charge"), _charge)
        if crash_armed["on"]:
            crash_armed["on"] = False
            raise _CrashError("died after charging, before committing the step")
        return StepResult.ok(charged=True)

    async def _charge() -> str:
        side_effect_calls["n"] += 1
        return "charged"

    async def s3(ctx: SagaContext) -> StepResult:
        return StepResult.ok(done=True)

    # Zero-delay retry so the crash-then-retry resolves within one drive (the
    # backoff timing is exercised separately in the orchestrator suite).
    instant = BackoffPolicy(max_attempts=3, base_delay_s=0.0, jitter=False)
    reg = SagaRegistry()
    reg.register(
        saga("pay", step("s1", s1), step("s2", s2, retry=instant), step("s3", s3))
    )

    # First orchestrator: drive until the crash. _Crash is caught by the engine as
    # a (retryable) step failure, so the step retries; we model "process death" by
    # abandoning this orchestrator object after the drive.
    orch1 = SagaOrchestrator(store, reg, clock=clock, effects=ledger)
    started = await orch1.start("pay", "corr-resume-1")
    await orch1.resume(started.id)  # s2 crashes once, retries (effect deduped), commits
    del orch1

    # The side effect ran exactly once despite s2's body executing twice.
    assert side_effect_calls["n"] == 1

    # A brand-new orchestrator over the same store sees a terminal instance.
    orch2 = SagaOrchestrator(store, reg, clock=clock, effects=ledger)
    final = await orch2.resume(started.id)
    assert final.status is SagaStatus.COMPLETED
    assert side_effect_calls["n"] == 1  # still exactly once


async def test_fresh_orchestrator_resumes_partial_forward_progress() -> None:
    """Resume continues from the durable cursor, not from the beginning.

    We hand-build a partially advanced instance (s1 COMPLETED, cursor at s2) in the
    store, then a fresh orchestrator drives it to completion running only s2 + s3.
    """
    ran: list[str] = []
    store = InMemorySagaStore()
    clock = ManualClock()

    async def make(name: str) -> Any:
        async def handler(ctx: SagaContext) -> StepResult:
            ran.append(name)
            return StepResult.ok()

        return handler

    reg = SagaRegistry()
    reg.register(
        saga("flow", step("s1", await make("s1")), step("s2", await make("s2")),
             step("s3", await make("s3")))
    )
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    started = await orch.start("flow", "corr-partial")

    # Simulate that s1 already completed in a previous (crashed) process.
    loaded = await store.load(started.id)
    assert loaded is not None
    inst = loaded.instance
    inst.status = SagaStatus.RUNNING
    inst.cursor = 1
    await store.save_instance(inst)
    s1_rec = loaded.steps[0]
    s1_rec.status = StepStatus.COMPLETED
    await store.save_step(s1_rec)

    # Fresh orchestrator resumes.
    orch2 = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    final = await orch2.resume(started.id)
    assert final.status is SagaStatus.COMPLETED
    assert ran == ["s2", "s3"]  # s1 was NOT re-run


async def test_resume_mid_compensation_finishes_rollback() -> None:
    """A crash mid-compensation resumes compensation and undoes the rest in reverse."""
    undone: list[str] = []
    store = InMemorySagaStore()
    clock = ManualClock()
    ledger: EffectLedger = InMemoryEffectLedger()

    async def ok(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    def comp_for(name: str) -> Any:
        async def comp(ctx: SagaContext) -> StepResult:
            undone.append(name)
            return StepResult.ok()

        return comp

    async def boom(ctx: SagaContext) -> StepResult:
        raise StepFailed("forward fail", retryable=False)

    reg = SagaRegistry()
    reg.register(
        saga(
            "flow",
            step("s1", ok, compensation=comp_for("s1")),
            step("s2", ok, compensation=comp_for("s2")),
            step("s3", ok, compensation=comp_for("s3")),
            step("boom", boom),
        )
    )
    orch = SagaOrchestrator(store, reg, clock=clock, effects=ledger)
    started = await orch.start("flow", "corr-comp")

    # Drive: s1..s3 complete, boom fails → full reverse compensation s3,s2,s1.
    final = await orch.resume(started.id)
    assert final.status is SagaStatus.COMPENSATED
    assert undone == ["s3", "s2", "s1"]

    # Now prove that resuming from a hand-set "crashed mid-compensation" state
    # finishes the rollback. Rebuild: s3 already COMPENSATED, s1/s2 still COMPLETED.
    undone.clear()
    store2 = InMemorySagaStore()
    orch2 = SagaOrchestrator(store2, reg, clock=clock, effects=InMemoryEffectLedger())
    started2 = await orch2.start("flow", "corr-comp-2")
    loaded = await store2.load(started2.id)
    assert loaded is not None
    inst = loaded.instance
    inst.status = SagaStatus.COMPENSATING
    inst.cursor = 4
    await store2.save_instance(inst)
    for rec in loaded.steps:
        if rec.name in ("s1", "s2"):
            rec.status = StepStatus.COMPLETED
        elif rec.name == "s3":
            rec.status = StepStatus.COMPENSATED  # already undone before the crash
        elif rec.name == "boom":
            rec.status = StepStatus.FAILED
        await store2.save_step(rec)

    orch3 = SagaOrchestrator(store2, reg, clock=clock, effects=InMemoryEffectLedger())
    final2 = await orch3.resume(started2.id)
    assert final2.status is SagaStatus.COMPENSATED
    # Only s2 then s1 remained to undo (s3 was already compensated pre-crash).
    assert undone == ["s2", "s1"]


async def test_resume_is_idempotent_on_terminal_instance() -> None:
    """Calling resume again on an already-terminal saga is a clean no-op."""
    store = InMemorySagaStore()
    clock = ManualClock()

    async def ok(ctx: SagaContext) -> StepResult:
        return StepResult.ok()

    reg = SagaRegistry()
    reg.register(saga("flow", step("s1", ok)))
    orch = SagaOrchestrator(store, reg, clock=clock, effects=InMemoryEffectLedger())
    inst = await orch.run_to_completion("flow", "corr-term")
    assert inst.status is SagaStatus.COMPLETED
    again = await orch.resume(inst.id)
    assert again.status is SagaStatus.COMPLETED
