"""The durable :class:`VideoJob` aggregate and its lifecycle vocabulary.

A ``VideoJob`` is the single source of truth for *one* asynchronous hosted-video
render, modelled so it survives a worker crash: every field needed to rehydrate
an in-flight task (provider, provider task id, idempotency key, poll deadline,
attempt counters, the persisted-asset pointer) lives on the row, never only in
process memory.

State machine (terminal states are absorbing)::

    submitted ──► running ──► succeeded   (asset persisted to object storage)
        │           │     └──► failed     (provider returned a hard error)
        │           └────────► expired    (provider URL/task TTL elapsed)
        └────────────────────► cancelled  (caller asked to stop)

``submitted`` and ``running`` are *in-flight*; the rest are *terminal*. The
distinction drives both :meth:`VideoJob.is_terminal` (the poller / await loop
stop condition) and crash recovery (only in-flight rows are rehydrated).

Everything here is pure data + pure predicates: no clock reads, no I/O. Time is
supplied by the engine (an injected :class:`~app.video.jobs.ports.JobClock`) so
transitions are reproducible in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    """The lifecycle state of a :class:`VideoJob`."""

    #: Accepted by us and submitted to the provider; awaiting the first poll/webhook.
    SUBMITTED = "submitted"
    #: The provider has acknowledged the task is generating.
    RUNNING = "running"
    #: The provider produced a clip URL **and** we persisted the asset durably.
    SUCCEEDED = "succeeded"
    #: The provider returned a hard, non-retryable failure.
    FAILED = "failed"
    #: The caller cancelled the job (best-effort provider cancel on top).
    CANCELLED = "cancelled"
    #: The provider task / result URL TTL elapsed before we could persist it.
    EXPIRED = "expired"


#: In-flight states: a worker must keep polling these and rehydrate them on boot.
INFLIGHT_STATES: frozenset[JobState] = frozenset({JobState.SUBMITTED, JobState.RUNNING})
#: Terminal (absorbing) states: no further transition is permitted.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED}
)

#: Legal forward transitions. Every value is reachable from ``SUBMITTED``;
#: terminal states have no outgoing edges. Re-entering the *same* in-flight state
#: (e.g. ``RUNNING`` → ``RUNNING`` on a later poll) is always allowed and handled
#: as a no-op by :meth:`VideoJob.can_transition`.
_ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.SUBMITTED: frozenset(
        {
            JobState.RUNNING,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.EXPIRED,
        }
    ),
    JobState.RUNNING: frozenset(
        {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.EXPIRED: frozenset(),
}


class JobTransitionError(RuntimeError):
    """Raised when an illegal state transition is attempted on a :class:`VideoJob`."""

    def __init__(self, *, job_id: str, src: JobState, dst: JobState) -> None:
        super().__init__(f"illegal video-job transition {src} -> {dst} (job={job_id})")
        self.job_id = job_id
        self.src = src
        self.dst = dst


@dataclass(frozen=True, slots=True)
class JobRequest:
    """A provider-agnostic render request.

    The ``payload`` is the opaque, provider-specific body (e.g. a serialized
    ``WanSpec`` for DashScope, or a MiniMax request). The engine never inspects
    it — it hands it to the provider's ``submit`` and stores it so a recovered
    job can be re-described for telemetry. ``idempotency_key`` makes re-submit
    safe: two submits with the same key collapse to one job.
    """

    provider: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    #: Optional duration hint (seconds) used for budget accounting + metrics.
    duration_s: float = 0.0
    #: Free-form correlation fields (shot_id, session_id, book_id, …). Stored,
    #: never sent to the provider by the engine.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobAsset:
    """A pointer to the durably-persisted clip in object storage.

    The provider's ``clip_url`` *expires*; this is the stable, owned location the
    rest of Kinora reads. ``sha256`` and ``size_bytes`` let a reader verify the
    object and let recovery detect a half-written upload.
    """

    storage_key: str
    sha256: str
    size_bytes: int
    content_type: str = "video/mp4"
    #: The (now-expiring) provider URL the bytes were fetched from, for provenance.
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class VideoJob:
    """The durable aggregate for one async video render.

    Instances are immutable; transitions return a *new* job via the ``with_*``
    helpers, so a repository can persist the new snapshot and the old one stays a
    valid record of the prior state (handy under optimistic concurrency).
    """

    id: str
    provider: str
    request: JobRequest
    state: JobState
    created_at: float
    updated_at: float
    #: The provider's async task handle (poll target / webhook correlation key).
    provider_task_id: str | None = None
    #: Wall-clock deadline (seconds, clock-relative) after which an unfinished job
    #: is declared :data:`JobState.EXPIRED`. ``None`` = no deadline yet (set at
    #: submit from the provider's backoff schedule).
    deadline_at: float | None = None
    #: Number of poll attempts performed (drives the backoff schedule + metrics).
    poll_attempts: int = 0
    #: Number of asset-download attempts (drives download retry + metrics).
    download_attempts: int = 0
    #: Set once the asset is persisted on success.
    asset: JobAsset | None = None
    #: Populated on FAILED / EXPIRED for diagnostics (never the API key).
    error: str | None = None
    #: Which path produced the terminal transition: ``"poll"`` | ``"webhook"`` |
    #: ``"deadline"`` | ``"cancel"``. Lets reconciliation reason about races.
    completed_by: str | None = None
    #: Optimistic-concurrency token; bumped on every mutating ``with_*`` call.
    version: int = 0

    # -- predicates ------------------------------------------------------- #

    @property
    def is_terminal(self) -> bool:
        """Whether the job has reached an absorbing state."""
        return self.state in TERMINAL_STATES

    @property
    def is_inflight(self) -> bool:
        """Whether a worker should keep polling / rehydrate this on boot."""
        return self.state in INFLIGHT_STATES

    def can_transition(self, dst: JobState) -> bool:
        """Whether moving to ``dst`` is legal (idempotent same-state re-entry ok)."""
        if dst == self.state:
            return dst in INFLIGHT_STATES  # re-asserting an in-flight state is fine
        return dst in _ALLOWED[self.state]

    def is_expired_at(self, now: float) -> bool:
        """Whether the deadline has elapsed at ``now`` (and the job is still open)."""
        return self.deadline_at is not None and now >= self.deadline_at and not self.is_terminal

    # -- transitions (return a new snapshot) ------------------------------ #

    def _evolve(self, *, now: float, **changes: Any) -> VideoJob:
        return replace(self, updated_at=now, version=self.version + 1, **changes)

    def transition(self, dst: JobState, *, now: float, **changes: Any) -> VideoJob:
        """Return a new job in state ``dst``; raise on an illegal edge."""
        if not self.can_transition(dst):
            raise JobTransitionError(job_id=self.id, src=self.state, dst=dst)
        return self._evolve(now=now, state=dst, **changes)

    def with_provider_task_id(self, task_id: str, *, now: float) -> VideoJob:
        """Stamp the provider's async task handle (idempotent if unchanged)."""
        if self.provider_task_id == task_id:
            return self
        return self._evolve(now=now, provider_task_id=task_id)

    def with_running(self, *, now: float, task_id: str | None = None) -> VideoJob:
        """Mark the provider as actively generating."""
        changes: dict[str, Any] = {}
        if task_id is not None:
            changes["provider_task_id"] = task_id
        return self.transition(JobState.RUNNING, now=now, **changes)

    def with_polled(self, *, now: float) -> VideoJob:
        """Record one more poll attempt (no state change)."""
        return self._evolve(now=now, poll_attempts=self.poll_attempts + 1)

    def with_download_attempt(self, *, now: float) -> VideoJob:
        """Record one more asset-download attempt (no state change)."""
        return self._evolve(now=now, download_attempts=self.download_attempts + 1)

    def with_succeeded(self, asset: JobAsset, *, now: float, completed_by: str) -> VideoJob:
        """Mark success once the asset is durably persisted."""
        return self.transition(
            JobState.SUCCEEDED, now=now, asset=asset, completed_by=completed_by, error=None
        )

    def with_failed(self, error: str, *, now: float, completed_by: str) -> VideoJob:
        """Mark a hard provider failure."""
        return self.transition(
            JobState.FAILED, now=now, error=error[:2000], completed_by=completed_by
        )

    def with_expired(self, *, now: float, completed_by: str, error: str | None = None) -> VideoJob:
        """Mark the job expired (deadline elapsed or provider TTL gone)."""
        return self.transition(
            JobState.EXPIRED,
            now=now,
            error=(error or "deadline elapsed before completion")[:2000],
            completed_by=completed_by,
        )

    def with_cancelled(self, *, now: float, reason: str | None = None) -> VideoJob:
        """Mark the job cancelled by the caller."""
        return self.transition(JobState.CANCELLED, now=now, error=reason, completed_by="cancel")


__all__ = [
    "INFLIGHT_STATES",
    "TERMINAL_STATES",
    "JobAsset",
    "JobRequest",
    "JobState",
    "JobTransitionError",
    "VideoJob",
]
