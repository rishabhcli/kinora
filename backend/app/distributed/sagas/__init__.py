"""The saga / process-manager engine (distributed-systems facet C).

Durable cross-service coordination for the Kinora backend. Two coordination
styles, one set of correctness primitives:

* **Orchestration** (:mod:`~app.distributed.sagas.orchestrator`) — a central
  :class:`SagaOrchestrator` drives a :class:`SagaDefinition` (an ordered list of
  ``step → compensation`` pairs) through a durable :class:`SagaStore`, persisting
  at every transition so a crash resumes correctly, and running compensations in
  reverse on failure (backward recovery).
* **Choreography** (:mod:`~app.distributed.sagas.choreography`) — no central
  driver; services react to events on a durable bus and emit the next event. A
  :class:`ProcessManager` correlates an event stream into saga progress.

Shared correctness machinery:

* **Exactly-once effects** (:mod:`~app.distributed.sagas.effects`) — an effect
  ledger turns at-least-once execution into exactly-once side effects via stable
  idempotency keys.
* **Distributed locks/leases with fencing** (:mod:`~app.distributed.sagas.locks`) —
  monotonic fencing tokens that a :class:`FencedResource` enforces, so a stalled
  old holder's write is rejected.
* **Timeouts / retries / dead-letter** — per-step backoff policies, per-invocation
  timeouts, an overall saga deadline, and a terminal FAILED state that is the
  engine's dead-letter.

Concrete sagas (:mod:`~app.distributed.sagas.flows`) model the
ingest→canon-build→identity-lock flow and the render→QA→conflict→degrade flow
(kinora.md §7.2, §9.7, §12) as compensatable sagas.

Durable backends live behind the store/ledger/lock protocols: the in-memory
references power the deterministic virtual-clock tests; the Redis + Postgres
variants (:mod:`~app.distributed.sagas.db_store`) carry production.
"""

from __future__ import annotations

from app.distributed.sagas.backoff import (
    DEFAULT_COMPENSATION_POLICY,
    DEFAULT_FORWARD_POLICY,
    BackoffPolicy,
    RetryDecision,
)
from app.distributed.sagas.choreography import (
    ChoreographyEvent,
    EventBus,
    InMemoryEventBus,
    ProcessManager,
    Reaction,
)
from app.distributed.sagas.composition import (
    SagaEngine,
    build_saga_engine,
    default_registry,
)
from app.distributed.sagas.definition import (
    SagaDefinition,
    SagaRegistry,
    SagaStep,
    UnknownSagaError,
    saga,
    step,
)
from app.distributed.sagas.effects import (
    EffectClaimStalled,
    EffectLedger,
    EffectRecord,
    EffectState,
    InMemoryEffectLedger,
    RedisEffectLedger,
)
from app.distributed.sagas.flows import (
    ArbitrationDecision,
    IngestPorts,
    RenderPorts,
    build_ingest_saga,
    build_render_saga,
)
from app.distributed.sagas.locks import (
    FencedResource,
    InMemoryLockManager,
    Lease,
    LockAcquireTimeout,
    LockManager,
    RedisLockManager,
    StaleFenceError,
)
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.runner import SagaWorker
from app.distributed.sagas.store import (
    InMemorySagaStore,
    LoadedSaga,
    SagaStats,
    SagaStore,
    StartResult,
)
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

__all__ = [
    "DEFAULT_COMPENSATION_POLICY",
    "DEFAULT_FORWARD_POLICY",
    "ArbitrationDecision",
    "BackoffPolicy",
    "ChoreographyEvent",
    "EffectClaimStalled",
    "EffectLedger",
    "EffectRecord",
    "EffectState",
    "EventBus",
    "FencedResource",
    "IngestPorts",
    "InMemoryEffectLedger",
    "InMemoryEventBus",
    "InMemoryLockManager",
    "InMemorySagaStore",
    "Lease",
    "LoadedSaga",
    "LockAcquireTimeout",
    "LockManager",
    "ProcessManager",
    "Reaction",
    "RedisEffectLedger",
    "RedisLockManager",
    "RenderPorts",
    "RetryDecision",
    "SagaContext",
    "SagaDefinition",
    "SagaEngine",
    "SagaInstance",
    "SagaOrchestrator",
    "SagaOutcome",
    "SagaRegistry",
    "SagaStats",
    "SagaStatus",
    "SagaStep",
    "SagaStore",
    "SagaWorker",
    "StaleFenceError",
    "StartResult",
    "StepDirection",
    "StepFailed",
    "StepHandler",
    "StepRecord",
    "StepResult",
    "StepStatus",
    "UnknownSagaError",
    "build_ingest_saga",
    "build_render_saga",
    "build_saga_engine",
    "default_registry",
    "saga",
    "step",
]
