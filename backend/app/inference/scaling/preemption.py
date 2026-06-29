"""Priority preemption under saturation (kinora.md §4.4, §4.8, §9.7).

Shedding (refusing new speculative work) is half the saturation story; the other
half is **preemption** — reclaiming a slot already occupied by lower-priority work
for an arriving higher-priority request. The §4.4 zones make the priority order
unambiguous: a **committed** render (full video the reader is seconds from
watching) outranks a **speculative** prefetch (a keyframe that degrades gracefully
to a Ken-Burns pan). When the fleet is full and a committed request arrives, the
right move is to *cancel the youngest speculative render* and hand its slot over —
exactly the cooperative cancellation §4.8 already requires of the render queue.

This module is the preemption planner. Given the set of in-flight jobs on a
saturated fleet and an arriving committed request, it decides whether to preempt
and *which* victim to choose:

* never preempt a committed job for another committed job (FIFO among equals — no
  priority inversion, no thrash);
* preempt only **speculative** victims, choosing the one that wastes the least
  work: the *youngest* (least elapsed service time), since cancelling a render that
  is 90% done throws away the most compute;
* respect a cancellation cost / minimum-progress guard so we don't preempt a job
  that will finish in the next instant anyway (preempting it costs more than
  waiting).

Pure planning over a job snapshot; the actual cancel is the queue's cooperative
token (§12.1), which the simulator models as re-queuing the victim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.inference.scaling.workload import RequestPriority

__all__ = [
    "PreemptionOutcome",
    "InflightJob",
    "PreemptionPolicy",
    "PreemptionDecision",
    "PreemptionPlanner",
]


class PreemptionOutcome(StrEnum):
    """What the preemption planner decided."""

    PREEMPT = "preempt"  # cancel a victim, give its slot to the arrival
    QUEUE = "queue"  # no eligible victim: the arrival waits
    NONE = "none"  # the arrival isn't high-priority enough to preempt


@dataclass(frozen=True, slots=True)
class InflightJob:
    """A job currently occupying a fleet slot (the preemption candidate set)."""

    job_id: str
    priority: RequestPriority
    #: Service time already elapsed on this job (sim-seconds).
    elapsed_s: float
    #: Total service time the job needs.
    total_s: float

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.total_s - self.elapsed_s)

    @property
    def progress(self) -> float:
        """Fraction complete in ``[0, 1]`` (work wasted if preempted)."""
        if self.total_s <= 0.0:
            return 1.0
        return min(1.0, self.elapsed_s / self.total_s)


@dataclass(frozen=True, slots=True)
class PreemptionPolicy:
    """Knobs for when preemption is worthwhile."""

    #: Don't preempt a victim already this fraction complete (let it finish).
    max_victim_progress: float = 0.8
    #: Don't preempt a victim whose remaining work is under this (it finishes soon).
    min_victim_remaining_s: float = 1.0
    #: Whether committed work may preempt speculative work at all.
    enabled: bool = True

    def __post_init__(self) -> None:
        if not 0.0 < self.max_victim_progress <= 1.0:
            raise ValueError("max_victim_progress must be in (0, 1]")
        if self.min_victim_remaining_s < 0.0:
            raise ValueError("min_victim_remaining_s must be non-negative")


@dataclass(frozen=True, slots=True)
class PreemptionDecision:
    """The planner's verdict for one arriving committed request."""

    outcome: PreemptionOutcome
    victim_id: str | None
    reason: str
    #: Work (sim-seconds) thrown away by the preemption (0 when not preempting).
    wasted_s: float = 0.0

    @property
    def preempted(self) -> bool:
        return self.outcome is PreemptionOutcome.PREEMPT

    def to_dict(self) -> dict[str, object]:
        """JSON projection."""
        return {
            "outcome": self.outcome.value,
            "victim_id": self.victim_id,
            "reason": self.reason,
            "wasted_s": round(self.wasted_s, 3),
        }


@dataclass(frozen=True, slots=True)
class PreemptionPlanner:
    """Chooses the least-wasteful speculative victim for a committed arrival."""

    policy: PreemptionPolicy = PreemptionPolicy()

    def plan(
        self,
        *,
        arrival_priority: RequestPriority,
        inflight: list[InflightJob],
        has_free_slot: bool,
    ) -> PreemptionDecision:
        """Decide whether (and whom) to preempt for an arriving request.

        Only a COMMITTED arrival onto a *full* fleet (no free slot) ever preempts;
        it targets the eligible speculative victim that wastes the least work (the
        youngest), respecting the progress + minimum-remaining guards.
        """
        if has_free_slot:
            return PreemptionDecision(
                outcome=PreemptionOutcome.NONE,
                victim_id=None,
                reason="free slot available: no preemption needed",
            )
        if not self.policy.enabled or arrival_priority is not RequestPriority.COMMITTED:
            return PreemptionDecision(
                outcome=PreemptionOutcome.NONE,
                victim_id=None,
                reason="arrival not eligible to preempt (speculative or disabled)",
            )

        eligible = [
            j
            for j in inflight
            if j.priority is RequestPriority.SPECULATIVE
            and j.progress < self.policy.max_victim_progress
            and j.remaining_s >= self.policy.min_victim_remaining_s
        ]
        if not eligible:
            return PreemptionDecision(
                outcome=PreemptionOutcome.QUEUE,
                victim_id=None,
                reason="no eligible speculative victim: committed request queues",
            )

        # Least wasted work = least elapsed (the youngest speculative render).
        victim = min(eligible, key=lambda j: j.elapsed_s)
        return PreemptionDecision(
            outcome=PreemptionOutcome.PREEMPT,
            victim_id=victim.job_id,
            reason="preempted youngest speculative render for committed request",
            wasted_s=victim.elapsed_s,
        )
