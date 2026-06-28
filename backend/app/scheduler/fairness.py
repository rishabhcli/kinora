"""Multi-reader fairness over one shared video budget (kinora.md §12.2/§11.1).

§12.2 requires **per-session fairness**: "a max concurrent render count per session
prevents one reader from starving others on shared workers." The budget itself
(§11.1) is a single pool of video-seconds (``budget_ceiling_video_s``, default
1,650) plus a per-session sub-cap (``budget_per_session_s``). When many readers
share one backend, a fast reader whose buffer keeps draining will keep requesting
promotions; without a fair allocator it can consume the whole pool and stall
everyone else.

This module is the **fair-share allocator**: given the live sessions, each with a
*buffer deficit* (how far below its low watermark it is — i.e. how urgently it
needs video) and a *weight* (e.g. priority/mode), it splits a shared video-second
allowance into a **per-session cap** that the §4.5 fill loop respects. The
allocation is:

* **Deficit-proportional with a max-min floor.** A session below its low watermark
  gets a share proportional to its deficit, but every needy session is guaranteed
  a minimum slice (max-min fairness) so a single huge-deficit reader can't take
  everything. Sessions already above their watermark request nothing and free
  their share for the needy.
* **Bounded by the per-session sub-cap.** No session is allocated more than
  ``budget_per_session_s`` worth of headroom in one round, mirroring §11.1.
* **Work-conserving.** Unused share (from satisfied sessions, or from sessions
  capped by their sub-cap) is redistributed to the still-needy in further rounds,
  so the pool is never left idle while a reader needs video.

**Spend invariant.** The allocator returns *caps*, not spend. The actual promotion
still goes through ``budget.reserve`` + ``can_render_live()`` downstream — the
allocator can only *lower* how much a session is allowed to request this round, so
the total allocated never exceeds the shared pool and the live-gate semantics are
untouched (with the gate off, fills don't fire regardless of the cap).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Floor share each *needy* session is guaranteed (fraction of the pool), so a
#: single large-deficit reader cannot crowd out the others entirely.
DEFAULT_MIN_SHARE_FRACTION = 0.1
#: Convergence tolerance for the water-filling redistribution (video-seconds).
_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class SessionDemand:
    """One session's claim on the shared budget this round (§12.2)."""

    session_id: str
    #: Video-seconds the session needs to refill to its high watermark
    #: (``H − committed_seconds_ahead`` for a draining session; 0 if satisfied).
    deficit_s: float
    #: Relative weight (e.g. by mode/priority); defaults to 1.0 (equal readers).
    weight: float = 1.0
    #: The §11.1 per-session sub-cap (max this session may hold). ``None`` = no cap.
    per_session_cap_s: float | None = None


@dataclass(frozen=True, slots=True)
class Allocation:
    """The per-session video-second caps for this round."""

    caps: dict[str, float]

    def cap_for(self, session_id: str) -> float:
        """The cap for ``session_id`` (0.0 if it was allocated nothing)."""
        return self.caps.get(session_id, 0.0)

    @property
    def total_s(self) -> float:
        return sum(self.caps.values())


class FairShareAllocator:
    """Split a shared video-second pool fairly across draining sessions (§12.2).

    Pure and deterministic: same demands + pool → same allocation. The algorithm
    is **weighted max-min water-filling** — repeatedly distribute the pool in
    proportion to each unsatisfied session's weight, clamp anyone who reaches
    their demand or sub-cap, and redistribute the freed remainder, until the pool
    is exhausted or every session is satisfied.
    """

    def __init__(self, *, min_share_fraction: float = DEFAULT_MIN_SHARE_FRACTION) -> None:
        self._min_share = max(0.0, min(1.0, min_share_fraction))

    def allocate(self, demands: list[SessionDemand], *, pool_s: float) -> Allocation:
        """Allocate up to ``pool_s`` video-seconds across ``demands`` (§12.2)."""
        needy = [d for d in demands if d.deficit_s > _EPSILON and d.weight > 0.0]
        if pool_s <= 0.0 or not needy:
            return Allocation(caps={d.session_id: 0.0 for d in demands})

        # Each session's hard ceiling this round = min(its deficit, its sub-cap).
        ceilings = {d.session_id: self._ceiling(d) for d in needy}
        weights = {d.session_id: d.weight for d in needy}
        allocated: dict[str, float] = {d.session_id: 0.0 for d in needy}

        remaining = pool_s
        # Optional max-min floor: guarantee each needy session a minimum slice up
        # front (bounded by its ceiling) before proportional water-filling.
        if self._min_share > 0.0:
            floor_each = (pool_s * self._min_share) / len(needy)
            for sid in list(weights):
                grant = min(floor_each, ceilings[sid])
                allocated[sid] += grant
                remaining -= grant
                if allocated[sid] + _EPSILON >= ceilings[sid]:
                    del weights[sid]

        # Weighted water-filling for the remainder.
        while remaining > _EPSILON and weights:
            total_w = sum(weights.values())
            if total_w <= 0.0:
                break
            progressed = False
            round_pool = remaining
            for sid in list(weights):
                share = round_pool * (weights[sid] / total_w)
                room = ceilings[sid] - allocated[sid]
                grant = min(share, room)
                if grant <= _EPSILON:
                    del weights[sid]
                    continue
                allocated[sid] += grant
                remaining -= grant
                progressed = True
                if allocated[sid] + _EPSILON >= ceilings[sid]:
                    del weights[sid]
            if not progressed:
                break

        caps = {d.session_id: 0.0 for d in demands}
        for sid, value in allocated.items():
            caps[sid] = round(value, 6)
        return Allocation(caps=caps)

    @staticmethod
    def _ceiling(demand: SessionDemand) -> float:
        if demand.per_session_cap_s is None:
            return demand.deficit_s
        return min(demand.deficit_s, demand.per_session_cap_s)


__all__ = [
    "DEFAULT_MIN_SHARE_FRACTION",
    "Allocation",
    "FairShareAllocator",
    "SessionDemand",
]
