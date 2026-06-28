"""Worker-pool autoscaling for the render lanes (kinora.md §12.2, §4.9).

§4.9 fixes the *steady-state* concurrency caps — 4 committed + 2 speculative + a
small keyframe pool — but a production deployment running many concurrent reading
sessions wants those pools to **breathe**: scale committed workers up when the
committed buffer falls behind (depth climbing) and back down when it drains, so
ECS/Function-Compute capacity tracks demand instead of sitting pinned at the cap.

This module is the *controller* — a pure function from observed queue state to a
desired worker count per lane — plus a small stateful wrapper that applies
min/max clamps and a **cooldown** so the pool can't flap (scale up, immediately
down, up again) on noisy depth readings. It deliberately does **not** spawn
processes: emitting a *desired* size keeps it testable and lets the orchestrator
(ECS service desired-count, a supervisor, or the worker's own TaskGroup) own the
actual scaling mechanism.

Policy per lane:

* **committed** scales with backlog: ``ceil((depth + inflight) / jobs_per_worker)``
  clamped to ``[min, max]`` — the buffer is sacred, so this lane gets the headroom.
* **speculative** scales gently and sheds first under pressure (it is droppable).
* **keyframe** stays a small fixed pool (cheap image lane) unless explicitly tuned.

The cooldown is *asymmetric*: scaling **up** is allowed immediately (a stalling
buffer is urgent), scaling **down** waits out the cooldown (avoid thrashing when a
burst is still settling) — the standard autoscaler bias toward availability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.db.models.enums import RenderPriority

logger = get_logger("app.queue.autoscale")

__all__ = ["LanePolicy", "AutoscalePlan", "LaneAutoscaler"]


@dataclass(frozen=True, slots=True)
class LanePolicy:
    """Scaling bounds + sensitivity for one lane."""

    min_workers: int
    max_workers: int
    jobs_per_worker: int = 2
    #: When True the lane scales with backlog; when False it is pinned to min.
    elastic: bool = True

    def __post_init__(self) -> None:
        if self.min_workers < 0 or self.max_workers < self.min_workers:
            raise ValueError("require 0 <= min_workers <= max_workers")
        if self.jobs_per_worker < 1:
            raise ValueError("jobs_per_worker must be >= 1")

    def desired_for(self, *, depth: int, inflight: int) -> int:
        """Target worker count for an observed backlog, clamped to the bounds."""
        if not self.elastic:
            return self.min_workers
        backlog = max(0, depth + inflight)
        if backlog == 0:
            return self.min_workers
        want = math.ceil(backlog / self.jobs_per_worker)
        return max(self.min_workers, min(self.max_workers, want))


def default_policies(
    *, committed_max: int = 8, speculative_max: int = 4, keyframe: int = 2
) -> dict[RenderPriority, LanePolicy]:
    """The §4.9 caps as elastic policies (committed gets the headroom)."""
    return {
        RenderPriority.COMMITTED: LanePolicy(
            min_workers=4, max_workers=committed_max, jobs_per_worker=1
        ),
        RenderPriority.SPECULATIVE: LanePolicy(
            min_workers=2, max_workers=speculative_max, jobs_per_worker=2
        ),
        RenderPriority.KEYFRAME: LanePolicy(
            min_workers=keyframe, max_workers=keyframe, jobs_per_worker=4, elastic=False
        ),
    }


@dataclass(frozen=True, slots=True)
class AutoscalePlan:
    """A desired worker count per lane plus the deltas from the current sizes."""

    desired: dict[RenderPriority, int]
    deltas: dict[RenderPriority, int]

    @property
    def total_desired(self) -> int:
        return sum(self.desired.values())

    @property
    def changed(self) -> bool:
        return any(d != 0 for d in self.deltas.values())


@dataclass
class LaneAutoscaler:
    """Stateful controller: observe queue depth → desired pool sizes (anti-flap).

    Holds the current per-lane sizes + a per-lane cooldown timestamp. ``plan``
    computes the next sizes: scale-up is immediate, scale-down waits out
    ``cooldown_s`` (asymmetric bias toward availability). Pass an explicit
    ``clock`` (seconds) so tests drive cooldown deterministically.
    """

    policies: dict[RenderPriority, LanePolicy]
    cooldown_s: float = 30.0
    current: dict[RenderPriority, int] = field(default_factory=dict)
    _last_scale: dict[RenderPriority, float] = field(default_factory=dict)
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.monotonic
        # Seed current sizes at each lane's minimum if not provided.
        for lane, policy in self.policies.items():
            self.current.setdefault(lane, policy.min_workers)

    def plan(self, observed: dict[RenderPriority, tuple[int, int]]) -> AutoscalePlan:
        """Compute desired sizes from ``{lane: (depth, inflight)}`` observations."""
        now = self.clock()
        desired: dict[RenderPriority, int] = {}
        deltas: dict[RenderPriority, int] = {}
        for lane, policy in self.policies.items():
            depth, inflight = observed.get(lane, (0, 0))
            target = policy.desired_for(depth=depth, inflight=inflight)
            cur = self.current.get(lane, policy.min_workers)
            if target > cur:
                next_size = target  # scale up immediately (a stalling buffer is urgent)
                self._last_scale[lane] = now
            elif target < cur:
                # Scale-down waits out the cooldown measured from the *last* scale
                # event (up or down) so a fresh burst that just landed isn't undone
                # the instant it drains — the anti-flap bias toward availability.
                last_scale = self._last_scale.get(lane, float("-inf"))
                if now - last_scale >= self.cooldown_s:
                    next_size = target
                    self._last_scale[lane] = now
                else:
                    next_size = cur  # within cooldown: hold
            else:
                next_size = cur
            desired[lane] = next_size
            deltas[lane] = next_size - cur
            self.current[lane] = next_size
        plan = AutoscalePlan(desired=desired, deltas=deltas)
        if plan.changed:
            logger.info(
                "autoscale.plan",
                desired={k.value: v for k, v in desired.items()},
                deltas={k.value: v for k, v in deltas.items()},
            )
        return plan

    async def observe(self, queue: Any) -> dict[RenderPriority, tuple[int, int]]:
        """Read ``(depth, inflight)`` per lane from a live queue (helper for ``plan``)."""
        out: dict[RenderPriority, tuple[int, int]] = {}
        for lane in self.policies:
            out[lane] = (await queue.depth(lane), await queue.inflight(lane))
        return out

    async def plan_from_queue(self, queue: Any) -> AutoscalePlan:
        """Observe a live queue and compute the next plan in one call."""
        return self.plan(await self.observe(queue))
