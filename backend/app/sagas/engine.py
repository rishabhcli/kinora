"""The durable saga / workflow execution engine.

This is the orchestrator. Given a :class:`~app.sagas.definition.Workflow` (looked
up by name in a :class:`~app.sagas.registry.WorkflowRegistry`) and a
:class:`~app.sagas.store.DurableStore`, it drives a run through its steps with
these guarantees:

**Durability / crash-resume.** State is written to the store *after every step*
(and on every timer/signal transition). A crash loses at most the in-flight
step; :meth:`SagaEngine.resume` reloads the persisted
:class:`~app.sagas.history.RunState` and continues from the cursor — it does not
restart from the top.

**Deterministic replay / idempotency.** A completed step is recorded with its
result. On a re-drive, a step already ``COMPLETED`` with the *same* idempotency
key is *not* re-executed — its recorded result is replayed (a ``step_skipped``
event). The idempotency key is attempt-invariant (see :mod:`app.sagas.ids`), so a
crash mid-step that re-runs the action passes the *same* key to the side effect,
which dedupes. Same history ⇒ same path, no double side effects.

**Saga compensation.** When a step fails past its retries (and isn't routed by a
branch), the engine flips to ``COMPENSATING`` and runs the compensations of the
already-completed steps **in reverse order**, best-effort: a compensation that
itself raises is recorded and the unwind continues. The run ends ``FAILED`` with
the collected post-mortem.

**Timers / signals.** A step may ``sleep`` (arm a durable timer) or
``await_signal`` (park until an external event arrives, with an optional
timeout that routes to a branch). A parked run persists as ``WAITING`` and is
re-driven by :meth:`SagaEngine.fire_due_timers` (the recovery sweep) or by
:meth:`SagaEngine.signal`.

**Per-attempt timeout.** Each attempt is raced against the step's
:class:`~app.sagas.policy.TimeoutPolicy` using the injected sleeper, so a hung
action is cancelled and retried/failed deterministically under a
:class:`~app.sagas.clock.FakeClock`.

The engine takes no real time and touches no real provider: time comes from the
injected :class:`~app.sagas.clock.Clock`, sleeps go through an injected async
sleeper, and the only side effects are the (injected) step actions. That is what
makes the whole subsystem deterministically testable with zero infra.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.sagas.clock import SYSTEM_CLOCK, Clock
from app.sagas.context import StepContext
from app.sagas.definition import END, Step, Workflow
from app.sagas.errors import (
    SagaError,
    SagaFailed,
    StepError,
    StoreConflictError,
    UnknownStepError,
)
from app.sagas.history import (
    CompensationOutcome,
    RunState,
    RunStatus,
    StepAttempt,
    StepOutcome,
    StepRecord,
    StepStatus,
    TimerState,
)
from app.sagas.ids import new_run_id, step_idempotency_key
from app.sagas.registry import WorkflowRegistry
from app.sagas.store import DurableStore
from app.sagas.telemetry import SagaEventType, TelemetryBus

logger = get_logger("app.sagas.engine")

#: An async sleeper the engine uses for backoff / timeout races. Production
#: passes ``asyncio.sleep``; tests pass a no-op that advances the FakeClock.
Sleeper = Callable[[float], Awaitable[None]]
#: A factory for fresh run ids (injectable for deterministic tests).
RunIdFactory = Callable[[], str]


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class SagaEngine:
    """Drives durable workflow runs against an injectable store + clock."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        store: DurableStore,
        *,
        clock: Clock = SYSTEM_CLOCK,
        sleeper: Sleeper = _real_sleep,
        bus: TelemetryBus | None = None,
        run_id_factory: RunIdFactory = new_run_id,
        lease_ttl_s: float = 300.0,
        owner: str = "engine",
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._sleep = sleeper
        self._bus = bus or TelemetryBus()
        self._new_id = run_id_factory
        self._lease_ttl = lease_ttl_s
        self._owner = owner

    # -- public API --------------------------------------------------------

    async def start(
        self, workflow: str, input: Any = None, *, run_id: str | None = None
    ) -> RunState:
        """Create and drive a new run to its next stable state.

        Returns the run state when it completes, fails (compensated), or parks
        on a timer/signal. Re-entrant: calling :meth:`resume` on the returned id
        continues a parked run.
        """
        wf = self._registry.get(workflow)
        now = self._clock.time()
        state = RunState(
            run_id=run_id or self._new_id(),
            workflow=workflow,
            status=RunStatus.PENDING,
            input=input,
            created_at=now,
            updated_at=now,
        )
        state = await self._store.create(state)
        self._emit(state, SagaEventType.RUN_STARTED)
        return await self._drive(wf, state)

    async def resume(self, run_id: str) -> RunState:
        """Reload a persisted run and continue driving it from its cursor."""
        state = await self._store.load(run_id)
        if state.is_terminal:
            return state
        wf = self._registry.get(state.workflow)
        self._emit(state, SagaEventType.RUN_RESUMED, cursor=state.cursor, status=state.status)
        return await self._drive(wf, state)

    async def signal(self, run_id: str, name: str, payload: Any = None) -> RunState:
        """Deliver an external event to a run; resume it if it was awaiting it.

        Idempotent at the run level: a signal for a run not (yet) waiting is
        stashed in ``pending_signals`` and consumed when the step reaches its
        await. Delivering the same signal again overwrites the stashed payload.
        """
        state = await self._store.load(run_id)
        if state.is_terminal:
            return state
        wf = self._registry.get(state.workflow)
        state.pending_signals[name] = payload
        self._emit(state, SagaEventType.SIGNAL_DELIVERED, signal=name)
        waiting_for = (
            state.timer.signal
            if state.status == RunStatus.WAITING and state.timer is not None
            else None
        )
        state = await self._persist(state)
        if waiting_for == name:
            return await self._drive(wf, state)
        return state

    async def cancel(self, run_id: str, *, reason: str = "cancelled") -> RunState:
        """Operator-cancel a run: compensate completed steps, then mark CANCELLED."""
        state = await self._store.load(run_id)
        if state.is_terminal:
            return state
        wf = self._registry.get(state.workflow)
        state.failure = reason
        state = await self._compensate(wf, state, failed_step=None)
        state.status = RunStatus.CANCELLED
        state = await self._persist(state)
        self._emit(state, SagaEventType.RUN_CANCELLED, reason=reason)
        return state

    async def fire_due_timers(self, *, limit: int = 100) -> list[RunState]:
        """Resume every WAITING run whose timer is due (the recovery sweep wakes them)."""
        now = self._clock.time()
        due = await self._store.due_runs(now, limit=limit)
        resumed: list[RunState] = []
        for parked in due:
            fire_at = parked.timer.fire_at if parked.timer else None
            self._emit(parked, SagaEventType.TIMER_FIRED, fire_at=fire_at)
            resumed.append(await self.resume(parked.run_id))
        return resumed

    # -- the driver --------------------------------------------------------

    async def _drive(self, wf: Workflow, state: RunState) -> RunState:
        """Execute steps from ``state.cursor`` until terminal or parked."""
        if state.status == RunStatus.PENDING:
            state.status = RunStatus.RUNNING
        state = await self._take_lease(state)

        while True:
            if state.is_terminal:
                return state
            step = wf.step_at(state.cursor)
            if step is None:
                # Ran past the last step → success.
                return await self._complete(state)

            try:
                state, advance = await self._run_step(wf, state, step)
            except _ParkRun:
                # The step armed a timer / await; state already persisted WAITING.
                return await self._store.load(state.run_id)
            except _CompensateRun as exc:
                state = await self._compensate(wf, state, failed_step=exc.step)
                state.status = RunStatus.FAILED
                state.failed_step = exc.step
                state.failure = exc.cause
                state = await self._persist(state)
                self._emit(state, SagaEventType.RUN_FAILED, step=exc.step, cause=exc.cause)
                raise SagaFailed(
                    state.run_id,
                    exc.step,
                    exc.cause,
                    compensated=state.compensated,
                    compensation_failures=state.compensation_failures,
                ) from None

            state.cursor = advance
            state = await self._persist(state)

    async def _run_step(
        self, wf: Workflow, state: RunState, step: Step
    ) -> tuple[RunState, int]:
        """Run (or replay) one step; return the (state, next-cursor).

        Raises :class:`_ParkRun` to park on a timer/signal, or
        :class:`_CompensateRun` when the step fails past retries.
        """
        rec = state.ensure_step(step.name)

        # --- replay: a completed step is never re-executed -----------------
        if rec.status == StepStatus.COMPLETED:
            self._emit(state, SagaEventType.STEP_SKIPPED, step=step.name, key=rec.idempotency_key)
            return state, self._next_cursor(wf, state, step)

        # --- signal await -------------------------------------------------
        signal_payload: Any = None
        if step.await_signal is not None:
            outcome = await self._resolve_await(wf, state, step, rec)
            if outcome.park:
                raise _ParkRun()  # persisted WAITING by _resolve_await
            if outcome.jump_to is not None:
                # await timed out and routed to a branch target.
                return state, outcome.jump_to
            signal_payload = outcome.payload

        # --- execute with retry + timeout ---------------------------------
        rec.status = StepStatus.RUNNING
        key = step_idempotency_key(
            state.run_id, step.name, state.input, salt=step.idempotency_salt
        )
        rec.idempotency_key = key
        state = await self._persist(state)
        self._emit(state, SagaEventType.STEP_STARTED, step=step.name, key=key)

        attempt = rec.attempt_count
        deadline = (
            self._clock.time() + step.timeout.total_s
            if step.timeout.total_s is not None
            else None
        )

        while True:
            attempt += 1
            started = self._clock.time()
            ctx = StepContext(
                state,
                step.name,
                attempt=attempt,
                idempotency_key=key,
                clock=self._clock,
                signal_payload=signal_payload,
            )
            try:
                result = await self._invoke_with_timeout(step, ctx)
            except (_ParkRun, _CompensateRun):
                # Internal control-flow — never treat as a step failure.
                raise
            except Exception as exc:  # noqa: BLE001 - any action error is a step failure
                # Classify: a StepError carries its own transient flag; any other
                # exception (a provider error, RuntimeError, …) is transient by
                # default. A PermanentStepError (or transient=False) is not retried.
                transient = exc.transient if isinstance(exc, StepError) else True
                timed_out = isinstance(exc, _TimeoutMarker)
                message = str(exc) or exc.__class__.__name__
                rec.attempts.append(
                    StepAttempt(
                        attempt=attempt,
                        started_at=started,
                        ended_at=self._clock.time(),
                        ok=False,
                        error=message,
                        transient=transient,
                        timed_out=timed_out,
                    )
                )
                state = await self._persist(state)
                if timed_out:
                    self._emit(state, SagaEventType.STEP_TIMEOUT, step=step.name, attempt=attempt)
                can_retry = step.retry.should_retry(attempt) and not self._past_deadline(deadline)
                if transient and can_retry:
                    backoff = step.retry.backoff_for(attempt, seed=key)
                    rec.status = StepStatus.RETRYING
                    state = await self._persist(state)
                    self._emit(
                        state,
                        SagaEventType.STEP_RETRYING,
                        step=step.name,
                        attempt=attempt,
                        backoff_s=backoff,
                    )
                    if backoff > 0:
                        await self._sleep(backoff)
                    continue
                # exhausted / permanent / past total deadline → fail this step
                rec.status = StepStatus.FAILED
                rec.outcome = StepOutcome.FAILED
                state = await self._persist(state)
                self._emit(state, SagaEventType.STEP_FAILED, step=step.name, attempt=attempt)
                raise _CompensateRun(step.name, message) from None
            else:
                rec.attempts.append(
                    StepAttempt(
                        attempt=attempt, started_at=started, ended_at=self._clock.time(), ok=True
                    )
                )
                rec.result = result
                rec.status = StepStatus.COMPLETED
                rec.outcome = StepOutcome.OK
                # consume the awaited signal now that the step succeeded
                if step.await_signal is not None:
                    state.pending_signals.pop(step.await_signal, None)
                state.timer = None
                state = await self._persist(state)
                self._emit(state, SagaEventType.STEP_COMPLETED, step=step.name)
                return state, self._next_cursor(wf, state, step)

    # -- branching ---------------------------------------------------------

    def _next_cursor(self, wf: Workflow, state: RunState, step: Step) -> int:
        """Resolve the next cursor for ``step`` honouring its branch."""
        if step.branch is None:
            return wf.index_of(step.name) + 1
        ctx = StepContext(
            state,
            step.name,
            attempt=0,
            idempotency_key=state.ensure_step(step.name).idempotency_key or "",
            clock=self._clock,
        )
        target = step.branch(ctx)
        if target is None:
            return wf.index_of(step.name) + 1
        if target == END:
            self._emit(state, SagaEventType.STEP_BRANCHED, step=step.name, target=END)
            return len(wf.steps)
        try:
            idx = wf.index_of(target)
        except SagaError as exc:
            raise UnknownStepError(f"branch from {step.name!r} → unknown {target!r}") from exc
        # mark skipped steps between current+1 and the branch target
        for skipped in wf.steps[wf.index_of(step.name) + 1 : idx]:
            srec = state.ensure_step(skipped.name)
            if srec.status == StepStatus.PENDING:
                srec.status = StepStatus.SKIPPED
                srec.outcome = StepOutcome.SKIPPED
        self._emit(state, SagaEventType.STEP_BRANCHED, step=step.name, target=target)
        return idx

    # -- signal await ------------------------------------------------------

    async def _resolve_await(
        self, wf: Workflow, state: RunState, step: Step, rec: StepRecord
    ) -> _AwaitResolution:
        """Decide how a signal-awaiting step proceeds.

        Returns one of:

        * payload present → ``_AwaitResolution(payload=...)`` (run the action);
        * not yet arrived → arm/keep a timer, persist WAITING, ``park=True``;
        * await timed out with ``on_await_timeout`` → ``jump_to=<cursor>`` (the
          step is recorded SKIPPED and the run jumps to the timeout target);
        * await timed out without a route → :class:`_CompensateRun`.
        """
        assert step.await_signal is not None
        signal = step.await_signal

        # Signal already delivered (now or stashed earlier) → run.
        if signal in state.pending_signals:
            self._emit(state, SagaEventType.SIGNAL_DELIVERED, step=step.name, signal=signal)
            return _AwaitResolution(payload=state.pending_signals[signal])

        now = self._clock.time()
        armed = (
            state.timer is not None
            and state.timer.step == step.name
            and state.timer.signal == signal
        )
        if armed:
            assert state.timer is not None
            if state.timer.fire_at <= now:
                # The await deadline elapsed without the signal.
                state.timer = None
                if step.on_await_timeout is not None:
                    rec.status = StepStatus.SKIPPED
                    rec.outcome = StepOutcome.SKIPPED
                    state.status = RunStatus.RUNNING
                    state = await self._persist(state)
                    target = (
                        len(wf.steps)
                        if step.on_await_timeout == END
                        else wf.index_of(step.on_await_timeout)
                    )
                    self._emit(
                        state,
                        SagaEventType.STEP_BRANCHED,
                        step=step.name,
                        target=step.on_await_timeout,
                        reason="await_timeout",
                    )
                    return _AwaitResolution(jump_to=target)
                raise _CompensateRun(step.name, f"await_signal {signal!r} timed out")
            # Still within the window → keep parking.
            return _AwaitResolution(park=True)

        # First time we reach this await → arm the timer (or park forever) and
        # persist WAITING so a restart re-discovers the parked run.
        fire_at = now + step.await_timeout_s if step.await_timeout_s is not None else float("inf")
        state.timer = TimerState(step=step.name, fire_at=fire_at, signal=signal)
        state.status = RunStatus.WAITING
        await self._persist(state)
        self._emit(
            state,
            SagaEventType.SIGNAL_WAIT,
            step=step.name,
            signal=signal,
            fire_at=fire_at,
        )
        return _AwaitResolution(park=True)

    # -- compensation ------------------------------------------------------

    async def _compensate(
        self, wf: Workflow, state: RunState, *, failed_step: str | None
    ) -> RunState:
        """Run completed steps' compensations in reverse, best-effort."""
        state.status = RunStatus.COMPENSATING
        state = await self._persist(state)
        # Completed steps, in the order they completed = definition order up to
        # the failed/cursor point. Reverse them.
        completed = [
            wf.step_by_name(r.name)
            for r in state.steps
            if r.status == StepStatus.COMPLETED
        ]
        for step in reversed(completed):
            rec = state.step_by_name(step.name)
            if rec is None or rec.compensation != CompensationOutcome.NONE:
                continue
            if step.compensation is None:
                continue
            self._emit(state, SagaEventType.COMPENSATION_STARTED, step=step.name)
            ctx = StepContext(
                state,
                step.name,
                attempt=0,
                idempotency_key=rec.idempotency_key or "",
                clock=self._clock,
                is_compensating=True,
            )
            try:
                await step.compensation(ctx)
            except Exception as exc:  # noqa: BLE001 - best-effort rollback
                rec.compensation = CompensationOutcome.FAILED
                rec.compensation_error = repr(exc)
                state.compensation_failures.append(step.name)
                self._emit(
                    state, SagaEventType.COMPENSATION_FAILED, step=step.name, error=repr(exc)
                )
            else:
                rec.compensation = CompensationOutcome.OK
                rec.status = StepStatus.COMPENSATED
                state.compensated.append(step.name)
                self._emit(state, SagaEventType.COMPENSATION_OK, step=step.name)
            state = await self._persist(state)
        return state

    # -- completion --------------------------------------------------------

    async def _complete(self, state: RunState) -> RunState:
        state.status = RunStatus.COMPLETED
        state.timer = None
        state.lease_until = None
        state.lease_owner = None
        state = await self._persist(state)
        self._emit(state, SagaEventType.RUN_COMPLETED)
        return state

    # -- timeout race ------------------------------------------------------

    async def _invoke_with_timeout(self, step: Step, ctx: StepContext) -> Any:
        """Run the action, racing it against a per-attempt deadline if set."""
        if step.timeout.per_attempt_s is None:
            return await step.action(ctx)
        action_task = asyncio.ensure_future(step.action(ctx))
        timer_task = asyncio.ensure_future(self._sleep(step.timeout.per_attempt_s))
        done, pending = await asyncio.wait(
            {action_task, timer_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if action_task in done:
            timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer_task
            return action_task.result()
        # timer won → cancel the action and report a timeout
        action_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await action_task
        raise _TimeoutMarker(f"step {step.name!r} exceeded {step.timeout.per_attempt_s}s")

    # -- helpers -----------------------------------------------------------

    def _past_deadline(self, deadline: float | None) -> bool:
        return deadline is not None and self._clock.time() >= deadline

    async def _take_lease(self, state: RunState) -> RunState:
        state.lease_until = self._clock.time() + self._lease_ttl
        state.lease_owner = self._owner
        return await self._persist(state)

    async def _persist(self, state: RunState) -> RunState:
        """Durably write ``state`` and return the **same** live object.

        The store keeps an isolated deep copy; the engine keeps driving the one
        in-memory ``state`` instance (and its step-record references stay valid)
        with its ``revision`` advanced in place. This is what lets a step mutate
        its record across several persists without re-fetching it each time.
        """
        state.updated_at = self._clock.time()
        try:
            stored = await self._store.save(state, expected_revision=state.revision)
        except StoreConflictError:
            # Another driver advanced this run; surface so the caller reloads.
            logger.warning("saga.persist.conflict", run_id=state.run_id, revision=state.revision)
            raise
        state.revision = stored.revision
        return state

    def _emit(self, state: RunState, type_: SagaEventType, **fields: Any) -> None:
        self._bus.emit(
            type_,
            state.run_id,
            state.workflow,
            step=fields.pop("step", None),
            **fields,
        )


# -- internal control-flow values & exceptions (never escape the engine) --


@dataclass(frozen=True, slots=True)
class _AwaitResolution:
    """How a signal-awaiting step proceeds (see :meth:`SagaEngine._resolve_await`)."""

    payload: Any = None
    park: bool = False
    jump_to: int | None = None


class _ParkRun(Exception):  # noqa: N818 - internal control-flow signal, not an error
    """Internal: the run parked on a timer/signal; state already persisted."""


class _CompensateRun(Exception):  # noqa: N818 - internal control-flow signal
    """Internal: a step failed past retries; trigger reverse compensation."""

    def __init__(self, step: str, cause: str) -> None:
        super().__init__(cause)
        self.step = step
        self.cause = cause


class _TimeoutMarker(StepError):  # noqa: N818 - internal marker, surfaced as StepTimeoutError
    """Internal: a per-attempt timeout, classified transient."""

    transient = True


__all__ = ["RunIdFactory", "SagaEngine", "Sleeper"]
