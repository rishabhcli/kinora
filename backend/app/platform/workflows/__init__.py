"""Kinora durable-execution engine — Temporal-style workflows for the backend.

A workflow is **deterministic, replayable code**: a normal ``async def`` whose
every interaction with the outside world goes through a
:class:`~app.platform.workflows.context.WorkflowContext` and is recorded as an
event in a durable history. On crash-recovery the engine *replays* the function
against the same history, so a resumed run is identical to one that never
crashed. On top of that primitive the engine provides:

* **Activities** — the non-deterministic I/O units, executed at-least-once with
  retries, start-to-close/heartbeat **timeouts**, and **heartbeating** (progress
  checkpointing for long renders/ingests).
* **Durable timers** — sleeps in workflow time that survive crashes for free.
* **Signals & queries** — async input into, and synchronous read-only views of, a
  running execution.
* **Child workflows & continue-as-new** — composition and unbounded-history relief.
* **Versioning / patching** — deploy new workflow code safely against in-flight
  histories without non-determinism.
* **Worker runtime + task queues** — the loops that turn the event/command model
  into durable progress; a deterministic test harness proves crash-resume ≡
  fresh-run.

This package is **additive** and composes on top of — never edits — the
operational jobs framework (:mod:`app.jobs`, whose :class:`Clock` it reuses) and
the shot render queue (:mod:`app.queue`). See ``DESIGN.md`` and kinora.md §9.7/§12.
Concrete durable workflows for the long book-ingest→render-scene pipeline and the
multi-agent "produce an episode" orchestration live in
:mod:`app.platform.workflows.defs`.
"""

from __future__ import annotations

from app.platform.workflows.activity import ActivityContext
from app.platform.workflows.client import WorkflowClient, WorkflowHandle
from app.platform.workflows.context import WorkflowContext, WorkflowInfo
from app.platform.workflows.errors import (
    ActivityCancelled,
    ActivityFailure,
    ActivityTimeout,
    ApplicationError,
    ChildWorkflowFailure,
    NonDeterminismError,
    WorkflowAlreadyExistsError,
    WorkflowCancelled,
    WorkflowError,
    WorkflowNotFoundError,
)
from app.platform.workflows.events import EventType, HistoryEvent
from app.platform.workflows.futures import WorkflowFuture, gather, wait_any
from app.platform.workflows.harness import (
    WorkflowTestEnvironment,
    assert_deterministic_replay,
)
from app.platform.workflows.memory_store import InMemoryWorkflowStore
from app.platform.workflows.registry import (
    DEFAULT_ACTIVITY_REGISTRY,
    DEFAULT_WORKFLOW_REGISTRY,
    ActivityRegistry,
    WorkflowRegistry,
    activity,
    workflow,
)
from app.platform.workflows.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from app.platform.workflows.store import (
    ExecutionStatus,
    WorkflowExecution,
    WorkflowStore,
)
from app.platform.workflows.worker import (
    ActivityTaskProcessor,
    TimerService,
    Worker,
    WorkerConfig,
    WorkflowTaskProcessor,
)

__all__ = [
    "DEFAULT_ACTIVITY_REGISTRY",
    "DEFAULT_RETRY_POLICY",
    "DEFAULT_WORKFLOW_REGISTRY",
    "ActivityCancelled",
    "ActivityContext",
    "ActivityFailure",
    "ActivityRegistry",
    "ActivityTaskProcessor",
    "ActivityTimeout",
    "ApplicationError",
    "ChildWorkflowFailure",
    "EventType",
    "ExecutionStatus",
    "HistoryEvent",
    "InMemoryWorkflowStore",
    "NonDeterminismError",
    "RetryPolicy",
    "TimerService",
    "Worker",
    "WorkerConfig",
    "WorkflowAlreadyExistsError",
    "WorkflowCancelled",
    "WorkflowClient",
    "WorkflowContext",
    "WorkflowError",
    "WorkflowExecution",
    "WorkflowFuture",
    "WorkflowHandle",
    "WorkflowInfo",
    "WorkflowNotFoundError",
    "WorkflowRegistry",
    "WorkflowStore",
    "WorkflowTaskProcessor",
    "WorkflowTestEnvironment",
    "activity",
    "assert_deterministic_replay",
    "gather",
    "wait_any",
    "workflow",
]
