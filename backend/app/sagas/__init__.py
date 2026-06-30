"""A durable saga / workflow engine for Kinora's multi-step flows.

Ingest (parse → keyframes → identity → canon) and the §9.7 per-shot render
(reserve → design → generate → normalize → persist → QA) are long, expensive,
side-effecting chains. A naive re-run after a crash double-spends video-seconds,
re-writes object storage, and can leave a half-imported book behind. This package
makes such flows **durable, resumable, and self-undoing**:

* a declarative **definition DSL** — :mod:`app.sagas.definition`: ordered,
  branching, compensable :class:`~app.sagas.definition.Step` s with per-step
  retry + timeout policy (:mod:`app.sagas.policy`);
* a **durable execution engine** — :mod:`app.sagas.engine`: persists run state to
  an injectable :class:`~app.sagas.store.DurableStore` after every step, so a
  crash **resumes from the last completed step**, with **deterministic replay**
  (a completed step is replayed from its recorded result, never re-executed) keyed
  by an **attempt-invariant idempotency key** (:mod:`app.sagas.ids`) so a
  re-driven side effect dedupes — same history ⇒ same path, no double effects;
* **saga compensation** — on a failure past the point of no return the engine runs
  the completed steps' compensations **in reverse**, best-effort and recorded;
* **timers / signals** — a step can await an external event with a timeout that
  routes to a branch, and a :class:`~app.sagas.recovery.RecoverySweeper` fires due
  timers and re-claims abandoned (expired-lease) runs;
* concrete **example workflows** — :mod:`app.sagas.workflows.ingest` and
  :mod:`app.sagas.workflows.render_shot`.

The whole subsystem reads *now* through an injected
:class:`~app.sagas.clock.Clock` and sleeps through an injected sleeper, and every
side effect is an injected callable — so it is exercised end-to-end with **zero
infra or network** under a :class:`~app.sagas.clock.FakeClock`. It lives entirely
under this namespace and adds no behaviour to existing modules.
"""

from __future__ import annotations

from app.sagas.clock import SYSTEM_CLOCK, Clock, FakeClock, SystemClock
from app.sagas.composition import SagaRuntime, build_saga_runtime
from app.sagas.context import StepContext
from app.sagas.definition import (
    END,
    Action,
    Branch,
    Compensation,
    Step,
    Workflow,
    WorkflowBuilder,
)
from app.sagas.engine import SagaEngine
from app.sagas.errors import (
    CompensationError,
    PermanentStepError,
    RunNotFoundError,
    SagaError,
    SagaFailed,
    StepError,
    StepTimeoutError,
    StoreConflictError,
    TransientStepError,
    UnknownStepError,
    UnknownWorkflowError,
    WorkflowDefinitionError,
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
from app.sagas.ids import fingerprint, new_run_id, step_idempotency_key
from app.sagas.policy import (
    DEFAULT_RETRY,
    NO_RETRY,
    NO_TIMEOUT,
    RetryPolicy,
    TimeoutPolicy,
)
from app.sagas.recovery import RecoverySweeper, SweepReport
from app.sagas.registry import WorkflowRegistry
from app.sagas.store import DurableStore, InMemoryDurableStore
from app.sagas.telemetry import (
    RecordingBus,
    SagaEvent,
    SagaEventType,
    TelemetryBus,
)

__all__ = [
    "DEFAULT_RETRY",
    "END",
    "NO_RETRY",
    "NO_TIMEOUT",
    "SYSTEM_CLOCK",
    "Action",
    "Branch",
    "Clock",
    "Compensation",
    "CompensationError",
    "CompensationOutcome",
    "DurableStore",
    "FakeClock",
    "InMemoryDurableStore",
    "PermanentStepError",
    "RecordingBus",
    "RecoverySweeper",
    "RetryPolicy",
    "RunNotFoundError",
    "RunState",
    "RunStatus",
    "SagaEngine",
    "SagaError",
    "SagaEvent",
    "SagaEventType",
    "SagaFailed",
    "SagaRuntime",
    "Step",
    "StepAttempt",
    "StepContext",
    "StepError",
    "StepOutcome",
    "StepRecord",
    "StepStatus",
    "StepTimeoutError",
    "StoreConflictError",
    "SweepReport",
    "SystemClock",
    "TelemetryBus",
    "TimeoutPolicy",
    "TimerState",
    "TransientStepError",
    "UnknownStepError",
    "UnknownWorkflowError",
    "Workflow",
    "WorkflowBuilder",
    "WorkflowDefinitionError",
    "WorkflowRegistry",
    "build_saga_runtime",
    "fingerprint",
    "new_run_id",
    "step_idempotency_key",
]
