"""The durable, event-sourced state of a workflow run.

A run's truth is its **history**: an append-only log of what happened plus a
small projected snapshot. Persisting the history after every event is what makes
a crash resumable (the engine replays the history to rebuild in-memory state and
continues from the last completed step) and what makes replay deterministic (the
same history drives the same decisions, and recorded step *results* are reused
instead of re-executing side effects).

Everything here is a frozen pydantic v2 model so it round-trips to/from JSON for
any :class:`~app.sagas.store.DurableStore` backend, and so a snapshot written by
one process is byte-stable and re-readable by another.

The vocabulary:

* :class:`RunStatus` — the run's lifecycle.
* :class:`StepOutcome` — how one step *ended* (vs :class:`StepStatus`, which is
  the step's *current* phase including in-flight).
* :class:`StepAttempt` — one execution attempt of one step (for retry audit).
* :class:`StepRecord` — the durable record of a step: its idempotency key, the
  retained result, attempts, and compensation outcome.
* :class:`RunState` — the whole run: definition name, input, ordered step
  records, pending timer/signal, cursor, status, and a monotone revision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    """Lifecycle of a workflow run."""

    PENDING = "pending"
    #: Steps are executing (or the run is between steps).
    RUNNING = "running"
    #: Blocked on a timer firing or a signal arriving.
    WAITING = "waiting"
    #: A step failed past retries and the engine is unwinding compensations.
    COMPENSATING = "compensating"
    #: Reached the end successfully.
    COMPLETED = "completed"
    #: Failed; compensation (if any) has run. Terminal.
    FAILED = "failed"
    #: Operator-cancelled; compensation (if any) has run. Terminal.
    CANCELLED = "cancelled"


#: Statuses from which no further progress is made.
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class StepStatus(StrEnum):
    """Current phase of a single step within a run."""

    PENDING = "pending"
    RUNNING = "running"
    #: Action raised; waiting for a backoff before the next attempt.
    RETRYING = "retrying"
    COMPLETED = "completed"
    #: A non-retryable failure or retries exhausted.
    FAILED = "failed"
    #: Skipped because a branch routed around it.
    SKIPPED = "skipped"
    #: This step's compensation has been executed (during rollback).
    COMPENSATED = "compensated"


class StepOutcome(StrEnum):
    """How a finished step ended (audit, distinct from in-flight status)."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class CompensationOutcome(StrEnum):
    """Result of running a step's compensation during rollback."""

    NONE = "none"
    OK = "ok"
    FAILED = "failed"


class StepAttempt(BaseModel):
    """One execution attempt of one step (for retry / timeout audit)."""

    model_config = ConfigDict(frozen=True)

    attempt: int = Field(ge=1)
    started_at: float
    ended_at: float | None = None
    ok: bool = False
    error: str | None = None
    transient: bool = True
    timed_out: bool = False


class StepRecord(BaseModel):
    """The durable record of a single step in a run.

    ``idempotency_key`` is set when the step *starts* and is what a downstream
    side effect dedupes against on resume. ``result`` is the (cheap,
    JSON-serialisable) value the action returned — replayed verbatim instead of
    re-running the action.
    """

    model_config = ConfigDict(frozen=False)

    name: str
    status: StepStatus = StepStatus.PENDING
    outcome: StepOutcome | None = None
    idempotency_key: str | None = None
    result: Any = None
    attempts: list[StepAttempt] = Field(default_factory=list)
    compensation: CompensationOutcome = CompensationOutcome.NONE
    compensation_error: str | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def is_done(self) -> bool:
        """Completed successfully — its result may be replayed."""
        return self.status == StepStatus.COMPLETED


class TimerState(BaseModel):
    """A pending durable timer (a ``sleep`` or a signal-await deadline)."""

    model_config = ConfigDict(frozen=True)

    #: The step that armed the timer.
    step: str
    #: Absolute deadline on the engine clock's ``time()`` scale.
    fire_at: float
    #: Optional signal the run is waiting for (None = a plain sleep).
    signal: str | None = None


class RunState(BaseModel):
    """The complete, durable state of one workflow run.

    Persisted after every meaningful transition. ``revision`` is bumped on each
    write and used by the store for optimistic concurrency / lease arbitration.
    """

    model_config = ConfigDict(frozen=False)

    run_id: str
    workflow: str
    status: RunStatus = RunStatus.PENDING
    #: The immutable workflow input (JSON-serialisable).
    input: Any = None
    #: Free-form, JSON-serialisable working state shared across steps.
    context: dict[str, Any] = Field(default_factory=dict)
    #: Ordered step records, one per definition step that has been reached.
    steps: list[StepRecord] = Field(default_factory=list)
    #: Index into the definition's step list of the *next* step to run.
    cursor: int = 0
    #: A pending timer/signal-await, if the run is WAITING.
    timer: TimerState | None = None
    #: Signals delivered but not yet consumed: name → most-recent payload.
    pending_signals: dict[str, Any] = Field(default_factory=dict)
    #: The step at which the run failed (for the post-mortem).
    failed_step: str | None = None
    #: A human-readable failure cause.
    failure: str | None = None
    #: Compensations that ran (in the order they ran — i.e. reverse of forward).
    compensated: list[str] = Field(default_factory=list)
    #: Compensations that themselves failed (best-effort rollback).
    compensation_failures: list[str] = Field(default_factory=list)
    #: Wall-clock-ish timestamps (engine clock scale) for the recovery sweep.
    created_at: float = 0.0
    updated_at: float = 0.0
    #: A lease deadline owned by a worker — used by the recovery sweep to detect
    #: an abandoned in-flight run (lease expired) without stealing a live one.
    lease_until: float | None = None
    lease_owner: str | None = None
    #: Monotone revision; bumped on every persisted write.
    revision: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def step_by_name(self, name: str) -> StepRecord | None:
        for rec in self.steps:
            if rec.name == name:
                return rec
        return None

    def ensure_step(self, name: str) -> StepRecord:
        """Get the record for ``name`` (creating a PENDING one if first reached)."""
        rec = self.step_by_name(name)
        if rec is None:
            rec = StepRecord(name=name)
            self.steps.append(rec)
        return rec


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "CompensationOutcome",
    "RunState",
    "RunStatus",
    "StepAttempt",
    "StepOutcome",
    "StepRecord",
    "StepStatus",
    "TimerState",
]
