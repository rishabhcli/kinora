"""Multi-provider, concurrency-aware promotion policy (kinora.md §4.9/§12.2).

The §4.9 control loop promotes shots one at a time until the buffer hits ``H`` or
the budget gate closes, with a fixed lane geometry (4 committed slots + 2
speculative). That is correct, but it leaves two levers unused that matter when
the buffer is draining faster than one provider can fill it:

1. **How many shots to release *this tick*.** Promoting one-at-a-time per event is
   fine when events are frequent, but at a buffer-drain burst (§4.10) the loop
   wants to fan out across *all* the free render slots at once — no more, no less.
   Releasing more than the free slots just queues work that can't start; releasing
   fewer under-fills and risks an underrun before the next event.

2. **Which provider / lane each promotion goes to.** With more than one hosted
   video provider available (e.g. the Wan and MiniMax backends the render
   pipeline can switch between), a fast-but-busy provider and a slow-but-idle one
   have different effective throughput. The policy spreads promotions to minimise
   the *time until the buffer is refilled to ``H``*, accounting for each provider's
   nominal latency and currently-free slots.

This module is a **pure planner**: it takes a snapshot of provider/lane capacity
and a list of budget-approved candidates (already filtered by
:func:`app.scheduler.optimizer.optimize_promotions` near the floor) and returns an
*assignment* — which candidate goes to which provider, in which lane, this tick.
It enqueues nothing and reserves nothing; the caller feeds the assignment into the
existing budget-gated enqueue path. It therefore spends **no** video-seconds the
``can_render_live()`` gate would not already allow — it only decides the *shape* of
the fan-out, never whether to spend.

Model
-----
* :class:`ProviderState` — one hosted provider's geometry: free committed slots,
  free speculative slots, and nominal per-shot latency (seconds). Latency is the
  knob that orders providers — a provider that finishes a shot in 8s is preferred
  over one that takes 20s when both have a free slot, because it lands sooner.
* :class:`PromotionPlan` — the per-tick assignment + the count promoted and the
  estimated reading-seconds until the buffer reaches ``H`` (the planner's own
  forecast, surfaced for the simulator's underrun accounting).
* :func:`plan_promotions` — the planner. Greedy by *soonest-landing slot*: it
  always assigns the next-most-urgent candidate to the free slot that will finish
  it earliest, which is optimal for minimising makespan on identical-value jobs
  with heterogeneous machine speeds (a classic list-scheduling result, and the
  values here are already urgency-ordered upstream).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import StrEnum

#: Default nominal per-shot render latency (seconds) when a provider does not
#: declare one — a conservative mid-range so the makespan forecast is sane.
DEFAULT_PROVIDER_LATENCY_S = 12.0


class Lane(StrEnum):
    """The render lanes a promotion can target (§4.9 concurrency caps)."""

    #: Full-video committed renders — these spend video-seconds and preempt.
    COMMITTED = "committed"
    #: Cheap keyframe/speculative renders — preemptible, droppable (no video-s).
    SPECULATIVE = "speculative"


@dataclass(frozen=True, slots=True)
class ProviderState:
    """A snapshot of one hosted provider's free capacity + speed (§4.9/§12.2).

    ``free_committed`` / ``free_speculative`` are the open slots in each lane right
    now (total slots minus in-flight). ``latency_s`` is the provider's nominal
    per-shot render time — the planner's ordering key, since a faster provider's
    free slot lands work sooner. ``healthy`` lets the policy route around a
    provider the resilience layer has tripped (a closed breaker) without removing
    it from the snapshot.
    """

    name: str
    free_committed: int = 0
    free_speculative: int = 0
    latency_s: float = DEFAULT_PROVIDER_LATENCY_S
    healthy: bool = True

    def free_in(self, lane: Lane) -> int:
        return self.free_committed if lane is Lane.COMMITTED else self.free_speculative


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """A budget-approved shot the planner may assign (urgency-ordered upstream)."""

    shot_id: str
    est_duration_s: float
    eta_s: float
    #: ``True`` ⇒ wants a full-video committed slot; ``False`` ⇒ a keyframe slot.
    committed: bool = True


@dataclass(frozen=True, slots=True)
class Assignment:
    """One planned promotion: which shot, to which provider, in which lane."""

    shot_id: str
    provider: str
    lane: Lane
    #: Estimated reading-seconds from now until this shot's clip lands.
    expected_landing_s: float


@dataclass(slots=True)
class PromotionPlan:
    """The per-tick fan-out plan (§4.9).

    ``assignments`` is the ordered list of promotions to enqueue this tick;
    ``deferred`` are urgent candidates that found no free slot (the caller holds
    them for the next event). ``makespan_s`` is the planner's estimate of when the
    *last* assigned shot lands — the forecast the simulator compares against the
    buffer-drain deadline to predict an underrun.
    """

    assignments: list[Assignment] = field(default_factory=list)
    deferred: list[PromotionCandidate] = field(default_factory=list)
    makespan_s: float = 0.0

    @property
    def promoted(self) -> int:
        return len(self.assignments)

    def for_provider(self, name: str) -> list[Assignment]:
        return [a for a in self.assignments if a.provider == name]

    def for_lane(self, lane: Lane) -> list[Assignment]:
        return [a for a in self.assignments if a.lane is lane]


def total_free_slots(providers: list[ProviderState], lane: Lane) -> int:
    """Free slots across all *healthy* providers in ``lane`` (the fan-out ceiling)."""
    return sum(p.free_in(lane) for p in providers if p.healthy)


def plan_promotions(
    candidates: list[PromotionCandidate],
    providers: list[ProviderState],
    *,
    max_parallel: int | None = None,
    drain_deadline_s: float | None = None,
) -> PromotionPlan:
    """Assign candidates to provider slots to minimise time-to-buffer-full (§4.9).

    Algorithm — *soonest-landing slot* list scheduling:

    * Maintain a min-heap of currently-free slots keyed by **when a shot placed
      there would land** (the provider's ``latency_s`` — every free slot starts
      immediately, so free slots run in parallel and each lands one latency out;
      the planner never over-commits past the free slots, so it never serialises).
    * Walk candidates in the urgency order they arrive (nearest-ETA first upstream)
      and pop the soonest-landing compatible slot for each. This greedily lands the
      most urgent work earliest, which minimises makespan for the homogeneous-value
      case the upstream optimiser already produced.
    * Stop at ``max_parallel`` (the hard fan-out cap) or when no compatible slot
      remains; remaining urgent candidates are ``deferred``.

    Lane compatibility: a ``committed`` candidate needs a committed slot, a
    speculative one a speculative slot — committed work never silently rides the
    cheap lane (it must spend its video-seconds under the gate the caller applies).

    ``drain_deadline_s`` (the reading-seconds until the buffer would hit empty) is
    advisory: when set, the plan's ``makespan_s`` can be compared to it to know
    whether the fan-out is *enough* to avert an underrun (the simulator uses this).
    It does **not** cause extra promotion beyond the free slots — the policy never
    invents capacity that isn't there.

    Pure: enqueues nothing, reserves nothing.
    """
    healthy = [p for p in providers if p.healthy]
    if not candidates or not healthy:
        return PromotionPlan(deferred=list(candidates))

    cap = _effective_cap(max_parallel, healthy)

    # Build a slot heap: (landing_time, tiebreak, provider_index, lane). Each
    # provider contributes `free_in(lane)` slots per lane; the k-th slot on a
    # provider/lane lands at (k+1) * latency because the slot serialises.
    heap: list[tuple[float, int, int, str]] = []
    tiebreak = 0
    for pi, p in enumerate(healthy):
        for lane in (Lane.COMMITTED, Lane.SPECULATIVE):
            for _ in range(p.free_in(lane)):
                # Every *currently-free* slot can start immediately, so it lands one
                # render-latency from now (free slots run in parallel — they do not
                # serialise; serialisation only happens when promoting past the free
                # slots, which this planner never does, it caps at the free count).
                heapq.heappush(heap, (p.latency_s, tiebreak, pi, lane.value))
                tiebreak += 1

    assignments: list[Assignment] = []
    deferred: list[PromotionCandidate] = []
    makespan = 0.0

    # We may need to skip-and-restore slots whose lane doesn't match the current
    # candidate; collect mismatches and push them back after each assignment.
    for cand in candidates:
        if len(assignments) >= cap:
            deferred.append(cand)
            continue
        want = Lane.COMMITTED if cand.committed else Lane.SPECULATIVE
        skipped: list[tuple[float, int, int, str]] = []
        placed = False
        while heap:
            landing, tb, pi, lane_value = heapq.heappop(heap)
            if lane_value != want.value:
                skipped.append((landing, tb, pi, lane_value))
                continue
            provider = healthy[pi]
            assignments.append(
                Assignment(
                    shot_id=cand.shot_id,
                    provider=provider.name,
                    lane=want,
                    expected_landing_s=round(landing, 6),
                )
            )
            makespan = max(makespan, landing)
            placed = True
            break
        for item in skipped:
            heapq.heappush(heap, item)
        if not placed:
            deferred.append(cand)

    plan = PromotionPlan(
        assignments=assignments,
        deferred=deferred,
        makespan_s=round(makespan, 6),
    )
    return plan


def _effective_cap(max_parallel: int | None, providers: list[ProviderState]) -> int:
    """The hard fan-out ceiling: ``max_parallel`` or all free slots if unset/≤0."""
    all_free = sum(p.free_committed + p.free_speculative for p in providers)
    if max_parallel is None or max_parallel <= 0:
        return all_free
    return min(max_parallel, all_free)


def covers_drain(plan: PromotionPlan, drain_deadline_s: float) -> bool:
    """Whether the plan lands enough video before the buffer drains (advisory).

    ``True`` when the planner's makespan is within the drain deadline — i.e. the
    last promoted clip is expected to land before the buffer would hit empty. Used
    by the simulator to attribute an underrun to *insufficient fan-out* rather than
    a closed budget gate.
    """
    if drain_deadline_s <= 0.0:
        return plan.promoted > 0
    return plan.promoted > 0 and plan.makespan_s <= drain_deadline_s


__all__ = [
    "DEFAULT_PROVIDER_LATENCY_S",
    "Assignment",
    "Lane",
    "PromotionCandidate",
    "PromotionPlan",
    "ProviderState",
    "covers_drain",
    "plan_promotions",
    "total_free_slots",
]
