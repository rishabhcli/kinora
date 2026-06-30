"""Domain vocabulary for distributed render orchestration (kinora.md §12.1/§12.2).

A single Redis priority queue drained by one render-worker process scales to a
handful of concurrent reads. A *production* deployment runs many render workers
across providers (Wan / MiniMax / a local lane) on ECS / Function-Compute, and
something has to decide **which worker renders which shot** — honouring the
worker's declared capabilities, the provider's remaining capacity, and locality
(keep a book's shots on one worker for visual continuity). That coordination
layer is :mod:`app.orchestration`; this module is its noun layer.

Everything here is a frozen pydantic-v2 model or a plain enum — no I/O, no clock,
no Redis. The coordinator and registry operate over these values through an
injectable broker/store (:mod:`app.orchestration.store`), so the whole subsystem
is exercised with an in-memory store + a virtual clock and **zero infra**.

The vocabulary maps onto the existing queue without duplicating it:

* a **lane** is the existing :class:`~app.db.models.enums.RenderPriority`
  (committed / speculative / keyframe) — the orchestrator never invents new lanes;
* a **shot ticket** (:class:`ShotTicket`) is the orchestrator's lightweight handle
  to a queued shot — its ``shot_hash`` is the §8.7 idempotency key, so the *same*
  identity that dedups the queue also dedups assignment here;
* a **lease** (:class:`ShotLease`) is the single-renderer guarantee: a shot is
  rendered by exactly one worker at a time, fenced by a monotonically increasing
  token so a resurrected zombie worker can never overwrite a reassignment.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import RenderPriority

__all__ = [
    "Lane",
    "ProviderId",
    "WorkerStatus",
    "WorkerCapabilities",
    "WorkerDescriptor",
    "ShotTicket",
    "ShotLease",
    "LeaseError",
    "FenceViolationError",
]

#: A render lane is exactly the queue's priority — committed / speculative /
#: keyframe. Aliased so orchestration code reads in its own vocabulary without
#: forking the enum.
Lane = RenderPriority

#: A provider identity (e.g. ``"wan"``, ``"minimax"``, ``"local"``, ``"keyframe"``).
#: Free-form so a new provider needs no enum edit; the registry matches on string.
ProviderId = str


class LeaseError(RuntimeError):
    """Base class for lease-protocol violations."""


class FenceViolationError(LeaseError):
    """A write was attempted with a stale fence token (a zombie worker).

    The single-renderer invariant is enforced by a monotonically increasing fence
    token stamped on every lease. When a lease expires and is reassigned the token
    advances; if the original (now presumed-dead) worker wakes up and tries to
    heartbeat / complete with its old token, the store rejects it. This is the
    classic fencing-token guard (Kleppmann) that makes lease expiry *safe* rather
    than merely hopeful.
    """


class WorkerStatus(enum.StrEnum):
    """Lifecycle of a registered worker as seen by the registry."""

    #: Registered, heartbeating, accepting assignments.
    ACTIVE = "active"
    #: Registered + heartbeating but voluntarily not taking new work (draining for
    #: a graceful shutdown / deploy). Existing leases are honoured to completion.
    DRAINING = "draining"
    #: Heartbeat lapsed past the TTL — leases are reclaimed and reassigned.
    DEAD = "dead"


class WorkerCapabilities(BaseModel):
    """What a worker can serve — the match key for assignment.

    A worker advertises the **lanes** it drains and the **providers** it can call
    (a GPU box might do the full Wan video lane; a cheap box only the keyframe
    image lane; a region-pinned box only its local provider). The coordinator
    assigns a ticket to a worker only if the worker's capabilities *cover* the
    ticket's lane + required provider — otherwise the shot would be claimed by a
    box that physically cannot render it.
    """

    model_config = ConfigDict(frozen=True)

    #: Lanes this worker drains. Empty = no lane (registered but inert).
    lanes: frozenset[Lane] = Field(default_factory=frozenset)
    #: Providers this worker can invoke. Empty = provider-agnostic (matches any).
    providers: frozenset[ProviderId] = Field(default_factory=frozenset)
    #: Max concurrent leases this worker holds (its local slot count, §4.9).
    max_concurrency: int = Field(default=1, ge=1)

    def can_serve(self, ticket: ShotTicket) -> bool:
        """True if this worker is *capable* of rendering ``ticket``.

        Capability is necessary but not sufficient — capacity/locality are layered
        on by the coordinator. A worker with no advertised providers is treated as
        provider-agnostic (it can call whatever the ticket needs).
        """
        if ticket.lane not in self.lanes:
            return False
        if not self.providers:
            return True
        return ticket.provider in self.providers


class WorkerDescriptor(BaseModel):
    """A registry record for one worker (its identity + last-known liveness).

    Immutable snapshot: the registry replaces the record wholesale on each
    heartbeat / status change rather than mutating in place, which keeps reads
    free of torn state under the in-memory store.
    """

    model_config = ConfigDict(frozen=True)

    worker_id: str
    capabilities: WorkerCapabilities
    status: WorkerStatus = WorkerStatus.ACTIVE
    #: Monotonic ms timestamp of the last heartbeat (virtual-clock friendly).
    last_heartbeat_ms: int = 0
    #: Optional region / zone tag for locality-aware placement.
    region: str | None = None

    def is_live(self, *, now_ms: int, ttl_ms: int) -> bool:
        """True if the heartbeat is within ``ttl_ms`` of ``now_ms``."""
        return self.status is not WorkerStatus.DEAD and (now_ms - self.last_heartbeat_ms) <= ttl_ms

    def accepts_work(self) -> bool:
        """True if the worker should be offered *new* assignments."""
        return self.status is WorkerStatus.ACTIVE


class ShotTicket(BaseModel):
    """The orchestrator's handle to one queued shot awaiting a worker.

    Carries only what placement needs — the §8.7 ``shot_hash`` idempotency key
    (which also dedups assignment), the lane, the provider the shot must be
    rendered with, and the ``book_id`` used for sticky locality. The heavy render
    payload stays in the existing queue; the orchestrator never re-materialises it.
    """

    model_config = ConfigDict(frozen=True)

    #: §8.7 content hash — the idempotency key. Two tickets with the same hash are
    #: the same shot and must never be assigned to two workers.
    shot_hash: str
    book_id: str
    lane: Lane
    provider: ProviderId
    #: Estimated video-seconds this shot will spend (drives provider-capacity
    #: accounting). Keyframe-lane tickets spend zero.
    video_seconds: float = Field(default=0.0, ge=0.0)
    #: Optional opaque queue job id, carried for the worker to claim the real job.
    job_id: str | None = None
    scene_id: str | None = None
    session_id: str | None = None

    @property
    def is_committed(self) -> bool:
        """Committed shots are sacred — never shed, preferentially placed."""
        return self.lane is Lane.COMMITTED


class ShotLease(BaseModel):
    """A worker's exclusive claim on one shot (the single-renderer guarantee).

    A lease binds ``shot_hash`` to ``worker_id`` until ``expires_at_ms``. The
    ``fence`` token advances every time the shot is (re)assigned; any write — a
    heartbeat extension or a completion — carrying a stale fence is rejected with
    :class:`FenceViolationError`. That is what makes lease *expiry* safe: a slow/zombie
    worker can lose its lease and never corrupt the worker that took over.
    """

    model_config = ConfigDict(frozen=True)

    shot_hash: str
    worker_id: str
    #: Monotonic fence token; strictly increasing across reassignments of a shot.
    fence: int = Field(ge=1)
    granted_at_ms: int
    expires_at_ms: int
    lane: Lane
    provider: ProviderId
    book_id: str

    def is_expired(self, *, now_ms: int) -> bool:
        return now_ms >= self.expires_at_ms

    def held_by(self, worker_id: str) -> bool:
        return self.worker_id == worker_id
