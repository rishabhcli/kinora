"""Value types, enums, and the handler contract for the jobs framework.

These are the small, dependency-free types the rest of the package is built on:

* :class:`JobRunStatus` / :class:`ScheduledJobState` — the durable run + job
  lifecycle states (stored as portable strings, mirrored by the ORM enums).
* :class:`TriggerKind` — discriminates the trigger flavours.
* :class:`RunOutcome` — what a handler reports back (success / skipped / failed).
* :class:`JobContext` — what a handler receives (its run, attempt, the clock,
  structured logger, and an opaque resource bag injected by the service).
* :class:`JobResult` — the structured return of a handler invocation.
* :class:`JobRun` — an in-flight/historical execution record (store value type).

The handler protocol is ``async (JobContext) -> JobResult | None``; returning
``None`` is treated as a plain success.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid an import cycle (clock imports nothing from here)
    from app.jobs.clock import Clock


class JobRunStatus(enum.StrEnum):
    """Lifecycle of a single scheduled-job *run* (one execution attempt set)."""

    PENDING = "pending"  # created, not yet due / not yet claimed
    RUNNING = "running"  # claimed by a worker, handler executing
    SUCCEEDED = "succeeded"  # handler returned success
    SKIPPED = "skipped"  # handler decided there was nothing to do (still terminal)
    RETRYING = "retrying"  # transient failure; re-queued after backoff
    FAILED = "failed"  # a terminal failure that did not dead-letter (cap=0 path)
    DEADLETTER = "deadletter"  # retries exhausted; parked in the DLQ
    CANCELLED = "cancelled"  # cancelled before/while running


#: Run statuses past which a run is no longer actionable by a worker.
TERMINAL_RUN_STATUSES: frozenset[JobRunStatus] = frozenset(
    {
        JobRunStatus.SUCCEEDED,
        JobRunStatus.SKIPPED,
        JobRunStatus.FAILED,
        JobRunStatus.DEADLETTER,
        JobRunStatus.CANCELLED,
    }
)


class ScheduledJobState(enum.StrEnum):
    """Whether a registered, scheduled job is actively being triggered."""

    ENABLED = "enabled"
    PAUSED = "paused"


class TriggerKind(enum.StrEnum):
    """The flavour of a job's trigger (discriminator for serialization/metrics)."""

    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"
    MANUAL = "manual"


class RunOutcome(enum.StrEnum):
    """What a handler reports: a plain result vocabulary independent of retries."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobResult:
    """The structured result of one handler invocation.

    ``outcome`` drives the dispatcher's ack/retry/DLQ decision: ``SUCCESS`` and
    ``SKIPPED`` are terminal-success, ``FAILED`` triggers the retry/DLQ path.
    ``detail`` is free-form structured data surfaced in logs + the durable record.
    """

    outcome: RunOutcome = RunOutcome.SUCCESS
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, **detail: Any) -> JobResult:
        """A success result with optional structured ``detail``."""
        return cls(outcome=RunOutcome.SUCCESS, detail=dict(detail))

    @classmethod
    def skipped(cls, reason: str, **detail: Any) -> JobResult:
        """A clean skip (nothing to do) — terminal, never retried."""
        return cls(outcome=RunOutcome.SKIPPED, detail={"reason": reason, **detail})

    @classmethod
    def failed(cls, error: str, **detail: Any) -> JobResult:
        """A handled failure that should follow the retry/DLQ path."""
        return cls(outcome=RunOutcome.FAILED, detail={"error": error, **detail})

    @property
    def is_success(self) -> bool:
        return self.outcome is RunOutcome.SUCCESS

    @property
    def is_skipped(self) -> bool:
        return self.outcome is RunOutcome.SKIPPED

    @property
    def is_failure(self) -> bool:
        return self.outcome is RunOutcome.FAILED


@dataclass(slots=True)
class JobRun:
    """A durable execution record for one run of a scheduled/enqueued job.

    ``idempotency_key`` is unique across active runs: re-creating a run with the
    same key is a no-op that returns the existing run (at-least-once dedup).
    ``scheduled_for`` is the instant the run became due; ``attempt`` is 1-based.
    """

    id: str
    job_name: str
    idempotency_key: str
    status: JobRunStatus
    scheduled_for: datetime
    created_at: datetime
    attempt: int = 0
    max_attempts: int = 1
    available_at: datetime | None = None  # earliest a worker may claim (backoff gate)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: RunOutcome | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    lease_token: str | None = None
    lease_until: datetime | None = None
    trigger_kind: TriggerKind = TriggerKind.MANUAL

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


@dataclass(slots=True)
class JobContext:
    """Everything a handler is handed when invoked.

    ``resources`` is the opaque bag the :class:`~app.jobs.service.JobService`
    injects (e.g. a session factory, the budget service, a search indexer). A
    maintenance handler reaches into it and *skips cleanly* when its dependency
    isn't present — this is what keeps the built-in registrations no-op-safe.
    """

    run: JobRun
    attempt: int
    clock: Clock
    logger: Any
    resources: Mapping[str, Any] = field(default_factory=dict)

    def resource(self, key: str, default: Any = None) -> Any:
        """Fetch an injected resource by key (``default`` when absent)."""
        return self.resources.get(key, default)


#: A job handler: ``async (JobContext) -> JobResult | None`` (None == success).
JobHandler = Callable[[JobContext], Awaitable["JobResult | None"]]


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "JobContext",
    "JobHandler",
    "JobResult",
    "JobRun",
    "JobRunStatus",
    "RunOutcome",
    "ScheduledJobState",
    "TriggerKind",
]
