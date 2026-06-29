"""Value types, enums, and the step contract for the saga engine.

These are the small, dependency-free types the rest of the package is built on.
A *saga* is a long-running, multi-step business transaction that cannot hold a
single ACID transaction across services, so it instead executes a sequence of
**steps**, each paired with a **compensation** that semantically undoes it. When a
step fails past its retry budget the engine runs the compensations of the already
completed steps in reverse — the classic saga *backward-recovery* pattern.

The vocabulary here is deliberately close to :mod:`app.jobs.types` (the operational
jobs framework) and :mod:`app.db.models.enums` (the render/shot state machine) so
the three coordination layers speak the same words:

* :class:`SagaStatus` — the lifecycle of a whole saga instance.
* :class:`StepStatus` — the lifecycle of one step within an instance.
* :class:`SagaOutcome` — the terminal verdict (committed / compensated / failed).
* :class:`StepDirection` — whether the engine is moving *forward* (executing steps)
  or *backward* (compensating). Stored on each step record so a crash mid-recovery
  resumes recovery, not forward progress.
* :class:`SagaContext` / :class:`StepResult` — what a step handler receives and
  returns.
* :class:`SagaInstance` / :class:`StepRecord` — the durable state value objects.

The handler contract is intentionally tiny: a step is an ``async`` callable that
takes a :class:`SagaContext` and returns a :class:`StepResult` (or ``None`` for a
plain success). Compensations share the same shape.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid an import cycle (clock/ledger import nothing from here)
    from app.distributed.sagas.effects import EffectLedger
    from app.jobs.clock import Clock


class SagaStatus(enum.StrEnum):
    """Lifecycle of a whole saga instance.

    The forward path is ``PENDING → RUNNING → COMPLETED``. A step failure that
    exhausts its retries flips the instance to ``COMPENSATING`` (running the
    completed steps' compensations in reverse); if every compensation succeeds it
    becomes ``COMPENSATED``, otherwise ``FAILED`` (a compensation itself could not
    be completed — the worst case, surfaced loudly). ``ABORTED`` is an operator/
    caller cancellation before completion.
    """

    PENDING = "pending"  # created, not yet started
    RUNNING = "running"  # executing forward steps
    COMPENSATING = "compensating"  # a step failed; undoing completed steps in reverse
    COMPLETED = "completed"  # all steps committed (forward success — terminal)
    COMPENSATED = "compensated"  # all compensations ran cleanly (terminal)
    FAILED = "failed"  # a compensation could not complete (terminal, needs attention)
    ABORTED = "aborted"  # cancelled before completion (terminal)
    TIMED_OUT = "timed_out"  # overall saga deadline elapsed (terminal-ish → compensates)


#: Saga statuses past which an instance is no longer actionable by the engine.
TERMINAL_SAGA_STATUSES: frozenset[SagaStatus] = frozenset(
    {
        SagaStatus.COMPLETED,
        SagaStatus.COMPENSATED,
        SagaStatus.FAILED,
        SagaStatus.ABORTED,
    }
)


class StepStatus(enum.StrEnum):
    """Lifecycle of a single step within a saga instance."""

    PENDING = "pending"  # not yet reached
    RUNNING = "running"  # forward handler executing
    COMPLETED = "completed"  # forward handler succeeded
    FAILED = "failed"  # forward handler exhausted retries (triggers compensation)
    COMPENSATING = "compensating"  # compensation handler executing
    COMPENSATED = "compensated"  # compensation handler succeeded
    COMPENSATION_FAILED = "compensation_failed"  # compensation exhausted retries (FATAL)
    SKIPPED = "skipped"  # step never ran (e.g. earlier failure) → nothing to compensate


#: Step statuses whose forward effect is "live" and therefore must be compensated
#: when the saga recovers backward.
COMPENSATABLE_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.COMPLETED, StepStatus.RUNNING}
)


class StepDirection(enum.StrEnum):
    """Which way the engine is currently driving a step record."""

    FORWARD = "forward"  # executing the step's action
    BACKWARD = "backward"  # executing the step's compensation


class SagaOutcome(enum.StrEnum):
    """The terminal verdict a finished saga reports, independent of retries."""

    COMMITTED = "committed"  # forward success
    COMPENSATED = "compensated"  # rolled back cleanly
    FAILED = "failed"  # rollback itself failed
    ABORTED = "aborted"  # cancelled


@dataclass(frozen=True, slots=True)
class StepResult:
    """The structured result of one step (or compensation) invocation.

    ``output`` is merged into the saga's shared ``state`` bag so downstream steps
    can read what an upstream step produced (e.g. step 1 yields a ``book_id`` step
    3 needs). ``retryable`` lets a handler signal a *terminal* failure that should
    skip the remaining retry budget and go straight to compensation (a poison
    input is not worth three more attempts).
    """

    output: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **output: Any) -> StepResult:
        """A success result, optionally publishing ``output`` into the saga state."""
        return cls(output=dict(output))


class StepFailed(Exception):  # noqa: N818 - public name in the handler contract
    """Raised by a step/compensation handler to signal a (possibly terminal) failure.

    The engine treats any raised exception as a failure, but ``StepFailed`` lets a
    handler be explicit and, via ``retryable=False``, demand that the engine stop
    retrying and move on (to compensation for a forward step, or to a fatal
    ``COMPENSATION_FAILED`` for a compensation).
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class StepRecord:
    """Durable per-step state within a saga instance.

    ``attempt`` counts forward attempts; ``comp_attempt`` counts compensation
    attempts (so a crash mid-compensation resumes the compensation with its own
    retry budget intact). ``available_at`` is the backoff gate shared by both
    directions — the engine never re-drives a step before this instant.
    """

    saga_id: str
    index: int  # 0-based position in the saga definition
    name: str
    status: StepStatus = StepStatus.PENDING
    direction: StepDirection = StepDirection.FORWARD
    attempt: int = 0
    comp_attempt: int = 0
    max_attempts: int = 3
    available_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    output: dict[str, Any] = field(default_factory=dict)

    @property
    def is_forward_done(self) -> bool:
        """Whether the forward action reached a settled (success/fail/skip) state."""
        return self.status in (
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.COMPENSATED,
            StepStatus.COMPENSATION_FAILED,
        )


@dataclass(slots=True)
class SagaInstance:
    """A durable record of one running/finished saga instance.

    ``correlation_id`` is the engine-level idempotency key: starting a saga twice
    with the same definition + correlation id returns the existing instance rather
    than launching a duplicate (the dedup that makes a re-delivered trigger safe).
    ``state`` is the shared bag steps read/write; ``cursor`` is the index of the
    next step to drive forward (or, while compensating, derived from completed
    steps). ``deadline`` is the optional overall-saga timeout.
    """

    id: str
    definition: str  # the registered saga definition name
    correlation_id: str
    status: SagaStatus = SagaStatus.PENDING
    cursor: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: SagaOutcome | None = None
    error: str | None = None
    deadline: datetime | None = None
    state: dict[str, Any] = field(default_factory=dict)
    available_at: datetime | None = None  # engine backoff gate at the instance level
    lease_token: str | None = None
    lease_until: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SAGA_STATUSES

    @property
    def is_compensating(self) -> bool:
        return self.status is SagaStatus.COMPENSATING


@dataclass(slots=True)
class SagaContext:
    """Everything a step handler is handed when invoked.

    ``state`` is the live, mutable shared bag — a handler reads upstream output
    from it and the engine merges the step's :class:`StepResult` ``output`` back
    into it after a successful return. ``effects`` is the exactly-once effect
    ledger: a handler wraps any non-idempotent side effect (an API call, a budget
    reserve) in ``ctx.effects.once(...)`` so a retry or crash-resume does not run
    it twice. ``resources`` is the opaque DI bag the engine injects (a session
    factory, the budget service, the canon store, …).
    """

    saga_id: str
    correlation_id: str
    step_name: str
    attempt: int
    direction: StepDirection
    clock: Clock
    state: dict[str, Any]
    effects: EffectLedger
    logger: Any
    resources: Mapping[str, Any] = field(default_factory=dict)

    def resource(self, key: str, default: Any = None) -> Any:
        """Fetch an injected resource by key (``default`` when absent)."""
        return self.resources.get(key, default)

    def effect_key(self, suffix: str) -> str:
        """A stable, per-saga effect-ledger key for this step.

        Namespaced by the saga instance + step so two instances of the same
        definition never collide, and a single step can mint several distinct
        effect keys (``ctx.effect_key("reserve")``, ``ctx.effect_key("submit")``).
        """
        return f"{self.saga_id}:{self.step_name}:{suffix}"


#: A step (or compensation) handler: ``async (SagaContext) -> StepResult | None``.
StepHandler = Callable[[SagaContext], Awaitable["StepResult | None"]]


__all__ = [
    "COMPENSATABLE_STEP_STATUSES",
    "TERMINAL_SAGA_STATUSES",
    "SagaContext",
    "SagaInstance",
    "SagaOutcome",
    "SagaStatus",
    "StepDirection",
    "StepFailed",
    "StepHandler",
    "StepRecord",
    "StepResult",
    "StepStatus",
]
