"""Admission control — backpressure + per-session fairness (kinora.md §12.2).

§12.2 names three guards on what the queue accepts:

1. **Backpressure.** When total queued depth crosses a threshold, *new
   speculative* enqueues are dropped (the keyframe ladder covers them); committed
   enqueues are always admitted. The queue's Lua already enforces this atomically
   on enqueue — this module mirrors the *decision* as a pure, testable function so
   callers (the Scheduler) can pre-check before paying the round-trip and surface
   "why was this dropped" telemetry.
2. **Per-session fairness.** A max concurrent render count per session stops one
   reader from monopolising the shared committed/speculative slots and starving
   everyone else. The queue tracks in-flight jobs per lane but not per session, so
   :class:`SessionFairness` maintains a per-session in-flight tally in Redis and
   the admission decision consults it.
3. **Lane policy.** Keyframe + committed bypass backpressure; speculative is the
   only droppable lane.

Everything here is pure-decision or thin-Redis so it is unit-testable against the
in-process fake with no infra. The decision type carries a machine-readable
``reason`` so the Scheduler can log *exactly* why an enqueue was admitted or shed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.db.models.enums import RenderPriority

logger = get_logger("app.queue.admission")

__all__ = [
    "AdmissionReason",
    "AdmissionDecision",
    "AdmissionController",
    "SessionFairness",
]


class AdmissionReason(StrEnum):
    """Why an admission decision went the way it did (machine-readable)."""

    ADMIT_COMMITTED = "admit_committed"
    ADMIT_KEYFRAME = "admit_keyframe"
    ADMIT_UNDER_LIMITS = "admit_under_limits"
    SHED_BACKPRESSURE = "shed_backpressure"
    SHED_SESSION_CAP = "shed_session_cap"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The outcome of an admission check."""

    admit: bool
    reason: AdmissionReason

    def __bool__(self) -> bool:
        return self.admit


def decide_admission(
    *,
    priority: RenderPriority,
    total_depth: int,
    backpressure_depth: int,
    session_inflight: int = 0,
    session_cap: int | None = None,
) -> AdmissionDecision:
    """Pure admission decision (no I/O), mirroring the queue's enqueue policy + fairness.

    * Committed is always admitted (the buffer must never stall).
    * Keyframe is always admitted (cheap image lane, covers degradation).
    * Speculative is shed under depth backpressure, then under the per-session cap.
    """
    if priority is RenderPriority.COMMITTED:
        return AdmissionDecision(True, AdmissionReason.ADMIT_COMMITTED)
    if priority is RenderPriority.KEYFRAME:
        return AdmissionDecision(True, AdmissionReason.ADMIT_KEYFRAME)
    # Speculative — the only droppable lane (§12.2).
    if total_depth >= backpressure_depth:
        return AdmissionDecision(False, AdmissionReason.SHED_BACKPRESSURE)
    if session_cap is not None and session_inflight >= session_cap:
        return AdmissionDecision(False, AdmissionReason.SHED_SESSION_CAP)
    return AdmissionDecision(True, AdmissionReason.ADMIT_UNDER_LIMITS)


class SessionFairness:
    """A per-session in-flight render tally in Redis (§12.2 fairness).

    The queue counts in-flight jobs per *lane*; fairness needs the count per
    *session*. This keeps a small ``SET``-backed tally per session keyed by the
    queue namespace, so admission can shed a speculative enqueue from a session
    that already holds ``session_cap`` slots while letting other readers through.

    It is best-effort and self-healing: ``acquire`` adds the job to the session's
    in-flight set, ``release`` removes it, and a sliding TTL bounds a leaked set if
    a job never releases (crash). The tally is *advisory* for speculative shedding,
    never a hard gate on committed work.
    """

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:rq",
        session_cap: int = 6,
        ttl_s: int = 21_600,
    ) -> None:
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._cap = session_cap
        self._ttl_s = ttl_s

    @property
    def cap(self) -> int:
        return self._cap

    def _key(self, session_id: str) -> str:
        return f"{self._ns}:session_inflight:{session_id}"

    async def inflight(self, session_id: str) -> int:
        """How many of ``session_id``'s renders are currently in flight."""
        return int(await self._redis.scard(self._key(session_id)))

    async def acquire(self, session_id: str, job_id: str) -> None:
        """Record ``job_id`` as in-flight for ``session_id`` (sliding TTL)."""
        key = self._key(session_id)
        await self._redis.sadd(key, job_id)
        await self._redis.expire(key, self._ttl_s)

    async def release(self, session_id: str, job_id: str) -> None:
        """Drop ``job_id`` from ``session_id``'s in-flight set (idempotent)."""
        await self._redis.srem(self._key(session_id), job_id)

    async def would_admit(self, session_id: str) -> bool:
        """True when the session is under its concurrent-render cap."""
        return await self.inflight(session_id) < self._cap


class AdmissionController:
    """Combines depth backpressure with per-session fairness for one decision.

    A thin orchestrator the Scheduler can call *before* enqueuing a speculative
    shot: it reads the current total depth + the session's in-flight tally and
    returns an :class:`AdmissionDecision`. Committed/keyframe short-circuit to
    admit without touching Redis (they never shed), so the hot committed path pays
    no extra round-trips.
    """

    def __init__(
        self,
        queue: Any,
        *,
        fairness: SessionFairness | None = None,
        backpressure_depth: int | None = None,
    ) -> None:
        self._queue = queue
        self._fairness = fairness
        # Default to the queue's own configured threshold so the pre-check matches
        # the Lua-enforced drop exactly.
        self._backpressure_depth = (
            backpressure_depth
            if backpressure_depth is not None
            else getattr(queue, "_backpressure_depth", 64)
        )

    async def check(
        self, *, priority: RenderPriority, session_id: str | None = None
    ) -> AdmissionDecision:
        """Decide whether a prospective enqueue should be admitted."""
        if priority in (RenderPriority.COMMITTED, RenderPriority.KEYFRAME):
            return decide_admission(
                priority=priority,
                total_depth=0,
                backpressure_depth=self._backpressure_depth,
            )
        total_depth = await self._queue.depth()
        session_inflight = 0
        session_cap = None
        if self._fairness is not None and session_id is not None:
            session_inflight = await self._fairness.inflight(session_id)
            session_cap = self._fairness.cap
        decision = decide_admission(
            priority=priority,
            total_depth=total_depth,
            backpressure_depth=self._backpressure_depth,
            session_inflight=session_inflight,
            session_cap=session_cap,
        )
        if not decision.admit:
            logger.info(
                "admission.shed",
                priority=priority.value,
                reason=decision.reason.value,
                session_id=session_id,
                total_depth=total_depth,
                session_inflight=session_inflight,
            )
        return decision
