"""Kinora's general scheduled-jobs & background-task framework.

A durable, distributed framework for *operational* background work on a cadence
(digest flushes, search-index refreshes, retention/GC sweeps, stuck-import
recovery, budget reconciliation) plus one-off enqueued tasks — **distinct** from
the shot render queue (:mod:`app.queue`) and the in-process scroll Scheduler
(:mod:`app.scheduler`). See ``app/jobs/DESIGN.md`` and kinora.md §4.7 / §12.

The public surface is intentionally small: build a :class:`JobService` (or drive
the pieces directly), register jobs with the :func:`job` decorator, and let the
scheduler+worker loops fire them under a distributed leader lease with
at-least-once, idempotent, retried-and-dead-lettered execution.
"""

from __future__ import annotations

from app.jobs.backoff import DEFAULT_POLICY, BackoffPolicy, RetryDecision
from app.jobs.clock import Clock, ManualClock, SystemClock
from app.jobs.cron import CronError, CronSchedule, parse_cron
from app.jobs.dispatcher import Dispatcher, DispatchResult
from app.jobs.harness import VirtualClockHarness
from app.jobs.lease import LeaderElector, LeaderLease
from app.jobs.maintenance import (
    MAINTENANCE_JOB_NAMES,
    default_maintenance_registry,
    register_maintenance_jobs,
)
from app.jobs.registry import (
    DEFAULT_REGISTRY,
    JobDefinition,
    JobRegistry,
    default_idempotency_key,
    job,
)
from app.jobs.runner import JobWorker
from app.jobs.scheduler import JobScheduler
from app.jobs.service import JobService, build_job_service
from app.jobs.store import EnqueueResult, InMemoryJobStore, JobStore, StoreStats
from app.jobs.triggers import (
    CronTrigger,
    IntervalTrigger,
    ManualTrigger,
    OnceTrigger,
    Trigger,
    cron,
    every,
    manual,
    once,
)
from app.jobs.types import (
    JobContext,
    JobHandler,
    JobResult,
    JobRun,
    JobRunStatus,
    RunOutcome,
    ScheduledJobState,
    TriggerKind,
)

__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_REGISTRY",
    "MAINTENANCE_JOB_NAMES",
    "BackoffPolicy",
    "Clock",
    "CronError",
    "CronSchedule",
    "CronTrigger",
    "DispatchResult",
    "Dispatcher",
    "EnqueueResult",
    "InMemoryJobStore",
    "IntervalTrigger",
    "JobContext",
    "JobDefinition",
    "JobHandler",
    "JobRegistry",
    "JobResult",
    "JobRun",
    "JobRunStatus",
    "JobScheduler",
    "JobService",
    "JobStore",
    "JobWorker",
    "LeaderElector",
    "LeaderLease",
    "ManualClock",
    "ManualTrigger",
    "OnceTrigger",
    "RetryDecision",
    "RunOutcome",
    "ScheduledJobState",
    "StoreStats",
    "SystemClock",
    "Trigger",
    "TriggerKind",
    "VirtualClockHarness",
    "build_job_service",
    "cron",
    "default_idempotency_key",
    "default_maintenance_registry",
    "every",
    "job",
    "manual",
    "once",
    "parse_cron",
    "register_maintenance_jobs",
]
