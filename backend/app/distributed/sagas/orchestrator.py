"""The orchestration engine — step → compensation, durable, crash-resumable.

This is the heart of the saga package. Given a :class:`SagaDefinition` and a
durable :class:`SagaStore`, the orchestrator drives an instance through its steps,
persisting state at **every** transition so a process crash at any point resumes
correctly. The contract it upholds:

**Forward progress.** Starting at the instance ``cursor``, run each step's action
with its retry/backoff policy and per-invocation timeout. On success, merge the
step's output into the shared state, mark the step COMPLETED, advance the cursor,
and persist — *then* move on. The persist-after-success ordering is what makes
resume safe: if we crash after running a step but before persisting COMPLETED, the
step re-runs on resume, and its side effects are protected by the effect ledger
(exactly-once), so the re-run is harmless.

**Backward recovery (compensation).** When a step's forward action exhausts its
retries (or fails non-retryably, or the saga deadline elapses), the instance flips
to COMPENSATING and the orchestrator runs the compensations of the already
COMPLETED steps **in reverse order**, each under its own retry policy. A step with
no compensation is skipped. If every compensation succeeds the instance becomes
COMPENSATED; if a compensation itself exhausts its retries the instance becomes
FAILED with that step marked COMPENSATION_FAILED (the loud, needs-attention state —
the system now holds a half-applied effect it could not undo).

**Crash-resume.** A "crash" loses the in-memory orchestrator but not the store.
:meth:`resume` (and the :class:`~app.distributed.sagas.runner.SagaWorker` loop that
calls it) rehydrates an instance from the store and continues from the durable
cursor / per-step status — forward if still RUNNING, backward if COMPENSATING. The
deterministic tests exercise this by dropping the orchestrator object mid-saga and
constructing a fresh one over the same store.

**Timeouts / retries / dead-letter.** Per-invocation timeouts and per-step retry
budgets come from the definition; the overall saga deadline triggers compensation.
A saga that lands in FAILED is the engine's dead-letter — it is terminal, retained
for inspection, and never silently retried.

The engine is driven against an injected :class:`~app.jobs.clock.Clock`, so the
backoff sleeps and deadline math run in *virtual* time under the manual clock and
the tests are exact (no real ``sleep``, no flakiness).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import structlog

from app.distributed.sagas import metrics
from app.distributed.sagas.backoff import RetryDecision
from app.distributed.sagas.definition import SagaDefinition, SagaRegistry, SagaStep
from app.distributed.sagas.effects import EffectLedger, InMemoryEffectLedger
from app.distributed.sagas.store import SagaStore, StartResult
from app.distributed.sagas.types import (
    SagaContext,
    SagaInstance,
    SagaOutcome,
    SagaStatus,
    StepDirection,
    StepFailed,
    StepHandler,
    StepRecord,
    StepResult,
    StepStatus,
)
from app.jobs.clock import Clock, SystemClock

_log = structlog.get_logger(__name__)


class SagaOrchestrator:
    """Drives saga instances forward/backward over a durable store.

    One orchestrator can drive many definitions (it resolves each instance's
    definition from the registry). It is stateless beyond its injected
    dependencies, so dropping and recreating it is exactly the crash-resume path.
    """

    def __init__(
        self,
        store: SagaStore,
        registry: SagaRegistry,
        *,
        clock: Clock | None = None,
        effects: EffectLedger | None = None,
        resources: Mapping[str, Any] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock or SystemClock()
        self._effects = effects or InMemoryEffectLedger(clock=self._clock)
        self._resources = dict(resources or {})

    @property
    def effects(self) -> EffectLedger:
        return self._effects

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(
        self,
        definition_name: str,
        correlation_id: str,
        *,
        initial_state: Mapping[str, Any] | None = None,
    ) -> SagaInstance:
        """Create (or dedup to an existing) instance of ``definition_name``.

        ``correlation_id`` is the engine-level idempotency key — starting twice
        with the same (definition, correlation_id) returns the existing instance.
        The instance is created PENDING; call :meth:`run_to_completion` or let a
        :class:`SagaWorker` drive it.
        """
        definition = self._registry.get(definition_name)
        now = self._clock.now()
        deadline = (
            now + timedelta(seconds=definition.deadline_s)
            if definition.deadline_s is not None
            else None
        )
        steps = [
            StepRecord(
                saga_id="",  # assigned by the store
                index=i,
                name=s.name,
                max_attempts=s.retry.max_attempts,
            )
            for i, s in enumerate(definition.steps)
        ]
        result: StartResult = await self._store.start(
            definition=definition_name,
            correlation_id=correlation_id,
            steps=steps,
            state=dict(initial_state or {}),
            deadline=deadline,
        )
        if result.created:
            metrics.saga_started(definition_name)
            # Stamp created_at/started_at now (the store left them None on create).
            inst = result.instance
            inst.created_at = inst.created_at or now
            await self._store.save_instance(inst)
        return result.instance

    async def run_to_completion(
        self,
        definition_name: str,
        correlation_id: str,
        *,
        initial_state: Mapping[str, Any] | None = None,
    ) -> SagaInstance:
        """Start (or dedup) then drive the saga until it reaches a terminal state."""
        instance = await self.start(
            definition_name, correlation_id, initial_state=initial_state
        )
        return await self.resume(instance.id)

    async def resume(self, saga_id: str) -> SagaInstance:
        """Drive (or re-drive) an instance from its durable state to terminal.

        Safe to call repeatedly and after a crash: it reloads the instance + steps
        and continues forward (RUNNING) or backward (COMPENSATING) from the
        persisted cursor/per-step status. Returns the terminal instance.
        """
        loaded = await self._store.load(saga_id)
        if loaded is None:
            raise KeyError(saga_id)
        instance = loaded.instance
        if instance.is_terminal:
            return instance
        if instance.status is not SagaStatus.PENDING:
            metrics.resumed()
        definition = self._registry.get(instance.definition)
        steps = loaded.steps

        if instance.status is SagaStatus.PENDING:
            instance.status = SagaStatus.RUNNING
            instance.started_at = instance.started_at or self._clock.now()
            await self._store.save_instance(instance)

        # Drive to terminal. Each loop body makes one logical transition then
        # re-reads its own in-memory state; persistence happens inside the helpers.
        while not instance.is_terminal:
            if self._deadline_exceeded(instance) and instance.status is SagaStatus.RUNNING:
                instance.status = SagaStatus.COMPENSATING
                instance.error = "saga deadline exceeded"
                metrics.saga_timed_out()
                await self._store.save_instance(instance)
                continue
            if instance.status is SagaStatus.RUNNING:
                await self._advance_forward(definition, instance, steps)
            elif instance.status in (SagaStatus.COMPENSATING, SagaStatus.TIMED_OUT):
                if instance.status is SagaStatus.TIMED_OUT:
                    instance.status = SagaStatus.COMPENSATING
                    await self._store.save_instance(instance)
                await self._advance_backward(definition, instance, steps)
            else:  # pragma: no cover - defensive
                break
        return instance

    async def abort(self, saga_id: str, *, reason: str = "aborted") -> SagaInstance | None:
        """Request compensation of a non-terminal saga (operator/caller cancel).

        Flips a RUNNING/PENDING instance to COMPENSATING so the next drive rolls
        back the completed steps; a no-op on a terminal instance. The actual
        rollback runs on the next :meth:`resume`.
        """
        loaded = await self._store.load(saga_id)
        if loaded is None:
            return None
        instance = loaded.instance
        if instance.is_terminal:
            return instance
        instance.status = SagaStatus.COMPENSATING
        instance.error = reason
        instance.outcome = SagaOutcome.ABORTED
        await self._store.save_instance(instance)
        metrics.saga_aborted()
        return instance

    # ------------------------------------------------------------------ #
    # Forward execution
    # ------------------------------------------------------------------ #
    async def _advance_forward(
        self, definition: SagaDefinition, instance: SagaInstance, steps: list[StepRecord]
    ) -> None:
        cursor = instance.cursor
        if cursor >= definition.step_count:
            # All steps completed → commit.
            instance.status = SagaStatus.COMPLETED
            instance.outcome = SagaOutcome.COMMITTED
            instance.finished_at = self._clock.now()
            await self._store.save_instance(instance)
            metrics.saga_committed()
            return

        step_def = definition.step_at(cursor)
        record = steps[cursor]
        record.direction = StepDirection.FORWARD

        # Honour the retry/backoff gate: if the step isn't available yet (a prior
        # attempt parked it), sleep until it is — but no longer than the saga
        # deadline, so an elapsing deadline pre-empts a long backoff at the loop top.
        if await self._await_gate(instance, record.available_at):
            return  # woke for the deadline; loop top will compensate

        outcome = await self._invoke(
            instance, step_def, record, direction=StepDirection.FORWARD
        )
        metrics.step_executed()
        if outcome.success:
            record.status = StepStatus.COMPLETED
            record.finished_at = self._clock.now()
            record.error = None
            record.available_at = None
            if outcome.output:
                record.output = dict(outcome.output)
                instance.state.update(outcome.output)
            instance.cursor = cursor + 1
            await self._store.save_step(record)
            await self._store.save_instance(instance)
            metrics.step_succeeded()
            return

        # Failure: consult the retry policy.
        decision = step_def.retry.decide(record.attempt) if outcome.retryable else (
            RetryDecision.GIVE_UP
        )
        if decision is RetryDecision.RETRY:
            delay = step_def.retry.delay_for(record.attempt + 1)
            record.status = StepStatus.PENDING
            record.available_at = self._clock.now() + timedelta(seconds=delay)
            record.error = outcome.error
            await self._store.save_step(record)
            # A failing-but-retryable step may have mutated the shared state bag
            # (e.g. the render saga re-renders a clip before raising to re-QA);
            # persist the instance so a crash between attempts resumes with it.
            await self._store.save_instance(instance)
            metrics.step_retried()
            return  # loop re-enters; the gate above sleeps the backoff next pass

        # Give up forward → mark failed and begin compensation.
        record.status = StepStatus.FAILED
        record.finished_at = self._clock.now()
        record.error = outcome.error
        await self._store.save_step(record)
        metrics.step_failed()
        instance.status = SagaStatus.COMPENSATING
        instance.error = f"step {step_def.name!r} failed: {outcome.error}"
        await self._store.save_instance(instance)

    # ------------------------------------------------------------------ #
    # Backward execution (compensation)
    # ------------------------------------------------------------------ #
    async def _advance_backward(
        self, definition: SagaDefinition, instance: SagaInstance, steps: list[StepRecord]
    ) -> None:
        # Find the highest-index step still needing compensation (COMPLETED, or a
        # COMPENSATING one we crashed mid-undo). Iterate in reverse.
        target: StepRecord | None = None
        for record in sorted(steps, key=lambda s: s.index, reverse=True):
            if record.status in (StepStatus.COMPLETED, StepStatus.COMPENSATING):
                target = record
                break

        if target is None:
            # Nothing left to undo → settle the terminal compensation state.
            self._settle_compensation(instance, steps)
            await self._store.save_instance(instance)
            return

        step_def = definition.step_at(target.index)
        if not step_def.has_compensation:
            # No undo for this step — it is logically compensated (a no-op).
            target.status = StepStatus.COMPENSATED
            target.finished_at = self._clock.now()
            await self._store.save_step(target)
            return

        # Honour the compensation backoff gate (set by a prior failed attempt).
        # Compensation is never deadline-pre-empted — we always want to finish
        # undoing — so we sleep the full remaining gate.
        if target.available_at is not None:
            now = self._clock.now()
            if now < target.available_at:
                await self._clock.sleep((target.available_at - now).total_seconds())

        target.status = StepStatus.COMPENSATING
        target.direction = StepDirection.BACKWARD
        await self._store.save_step(target)

        outcome = await self._invoke(
            instance, step_def, target, direction=StepDirection.BACKWARD
        )
        if outcome.success:
            target.status = StepStatus.COMPENSATED
            target.finished_at = self._clock.now()
            target.error = None
            target.available_at = None
            if outcome.output:
                instance.state.update(outcome.output)
            await self._store.save_step(target)
            await self._store.save_instance(instance)
            metrics.step_compensated()
            return

        decision = (
            step_def.compensation_retry.decide(target.comp_attempt)
            if outcome.retryable
            else RetryDecision.GIVE_UP
        )
        if decision is RetryDecision.RETRY:
            delay = step_def.compensation_retry.delay_for(target.comp_attempt + 1)
            target.status = StepStatus.COMPENSATING  # stays in-flight; re-driven next loop
            target.available_at = self._clock.now() + timedelta(seconds=delay)
            target.error = outcome.error
            await self._store.save_step(target)
            metrics.step_retried()
            return  # loop re-enters; the gate above sleeps the backoff next pass

        # Compensation exhausted → FATAL. Mark and fail the saga loudly.
        target.status = StepStatus.COMPENSATION_FAILED
        target.finished_at = self._clock.now()
        target.error = outcome.error
        await self._store.save_step(target)
        metrics.compensation_failed()
        instance.status = SagaStatus.FAILED
        instance.outcome = SagaOutcome.FAILED
        instance.finished_at = self._clock.now()
        instance.error = (
            f"compensation for step {step_def.name!r} failed: {outcome.error}"
        )
        await self._store.save_instance(instance)
        metrics.saga_failed()

    def _settle_compensation(self, instance: SagaInstance, steps: list[StepRecord]) -> None:
        any_fatal = any(s.status is StepStatus.COMPENSATION_FAILED for s in steps)
        instance.finished_at = self._clock.now()
        if any_fatal:
            instance.status = SagaStatus.FAILED
            instance.outcome = SagaOutcome.FAILED
            metrics.saga_failed()
        else:
            instance.status = SagaStatus.COMPENSATED
            instance.outcome = instance.outcome or SagaOutcome.COMPENSATED
            metrics.saga_compensated()

    # ------------------------------------------------------------------ #
    # Single invocation (with timeout + state mutation discipline)
    # ------------------------------------------------------------------ #
    async def _invoke(
        self,
        instance: SagaInstance,
        step_def: SagaStep,
        record: StepRecord,
        *,
        direction: StepDirection,
    ) -> _Outcome:
        handler: StepHandler
        if direction is StepDirection.FORWARD:
            handler = step_def.action
            record.attempt += 1
            attempt = record.attempt
            record.status = StepStatus.RUNNING
        else:
            # The caller only drives a step backward when it has a compensation.
            assert step_def.compensation is not None
            handler = step_def.compensation
            record.comp_attempt += 1
            attempt = record.comp_attempt
        record.started_at = record.started_at or self._clock.now()
        await self._store.save_step(record)

        ctx = SagaContext(
            saga_id=instance.id,
            correlation_id=instance.correlation_id,
            step_name=step_def.name,
            attempt=attempt,
            direction=direction,
            clock=self._clock,
            state=instance.state,
            effects=self._effects,
            logger=_log.bind(saga=instance.definition, step=step_def.name, attempt=attempt),
            resources=self._resources,
        )

        try:
            coro = handler(ctx)
            if step_def.timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=step_def.timeout_s)
            else:
                result = await coro
        except TimeoutError:
            return _Outcome(success=False, retryable=True, error="invocation timed out")
        except StepFailed as exc:
            return _Outcome(success=False, retryable=exc.retryable, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - any failure is a step failure
            return _Outcome(success=False, retryable=True, error=f"{type(exc).__name__}: {exc}")

        output = result.output if isinstance(result, StepResult) else {}
        return _Outcome(success=True, output=output)

    def _deadline_exceeded(self, instance: SagaInstance) -> bool:
        return instance.deadline is not None and self._clock.now() >= instance.deadline

    async def _await_gate(self, instance: SagaInstance, available_at: datetime | None) -> bool:
        """Sleep until ``available_at``, capped by the saga deadline.

        Returns ``True`` if the wait ended because the saga deadline arrived first
        (the caller must then *not* run the step — the loop top will compensate),
        ``False`` if the gate is open (the step may run). Sleeping in capped
        chunks is what lets an elapsing overall deadline pre-empt a long per-step
        backoff instead of being trapped behind it.
        """
        if available_at is None:
            return False
        now = self._clock.now()
        if now >= available_at:
            return False
        gate_wait = (available_at - now).total_seconds()
        if instance.deadline is not None:
            until_deadline = (instance.deadline - now).total_seconds()
            if until_deadline <= 0:
                return True
            if until_deadline < gate_wait:
                await self._clock.sleep(until_deadline)
                return self._clock.now() >= instance.deadline
        await self._clock.sleep(gate_wait)
        # If the deadline also passed during the gate sleep, signal it.
        return self._deadline_exceeded(instance)


class _Outcome:
    """Internal result of one handler invocation (success / retryable failure)."""

    __slots__ = ("success", "retryable", "error", "output")

    def __init__(
        self,
        *,
        success: bool,
        retryable: bool = True,
        error: str | None = None,
        output: Mapping[str, Any] | None = None,
    ) -> None:
        self.success = success
        self.retryable = retryable
        self.error = error
        self.output: Mapping[str, Any] = output or {}


__all__ = ["SagaOrchestrator"]
