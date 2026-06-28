"""Quality↔budget render-mode optimizer (kinora.md §12.4, §11.1, §4.4).

The degradation ladder (§12.4) gives every shot a choice of *rungs*, each cheaper
and lower-fidelity than the last:

    full Wan video  →  generated keyframe + Ken-Burns  →  the book's own
    illustration (Ken-Burns)  →  plain narrated text (audio only)

Only the top rung spends video-seconds (§4.4); the rest are ~free against the
scarce budget. Given a set of upcoming shots and a remaining video-seconds cap,
the optimizer answers: **which rung should each shot get to maximize total
delivered quality without breaching the cap?**

This is a bounded multiple-choice knapsack (each shot picks exactly one rung; the
"weight" is the rung's video-seconds, the "value" is its quality). The video
budget is small and rung costs are quantized to a tenth of a second, so an exact
DP is cheap and is what we use; a greedy marginal-quality-per-second pass provides
a fast, monotone fallback and a sanity check.

Pure, deterministic, no I/O. The chosen plan is reported with its total quality,
total video-seconds, and per-shot rung so the scheduler/HUD can act on it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class RenderRung(enum.StrEnum):
    """A rung on the §12.4 degradation ladder (most → least fidelity)."""

    FULL_VIDEO = "full_video"
    KEYFRAME_KENBURNS = "keyframe_kenburns"
    ILLUSTRATION_KENBURNS = "illustration_kenburns"
    TEXT_AUDIO = "text_audio"

    @property
    def rank(self) -> int:
        """0 = highest fidelity. Lower rank = better picture, more cost."""
        return _RUNG_RANK[self]


_RUNG_RANK: dict[RenderRung, int] = {
    RenderRung.FULL_VIDEO: 0,
    RenderRung.KEYFRAME_KENBURNS: 1,
    RenderRung.ILLUSTRATION_KENBURNS: 2,
    RenderRung.TEXT_AUDIO: 3,
}

#: Default delivered-quality weight per rung in [0, 1]. Full video is the
#: reference (1.0); each rung down delivers less of the "film" experience but the
#: bottom rung still delivers the read-along value (§12.4), so it is not zero.
DEFAULT_RUNG_QUALITY: dict[RenderRung, float] = {
    RenderRung.FULL_VIDEO: 1.0,
    RenderRung.KEYFRAME_KENBURNS: 0.6,
    RenderRung.ILLUSTRATION_KENBURNS: 0.45,
    RenderRung.TEXT_AUDIO: 0.25,
}


@dataclass(frozen=True, slots=True)
class ShotOption:
    """A shot's choices: its full-video cost + the per-rung quality available.

    ``video_seconds`` is the cost of the *full-video* rung (the only rung that
    spends budget); all cheaper rungs cost 0 video-seconds (§4.4). ``importance``
    scales every rung's quality — a pivotal beat is worth more film than a
    transition — so the optimizer spends the budget where it matters most.
    """

    shot_id: str
    video_seconds: float
    importance: float = 1.0
    rung_quality: dict[RenderRung, float] = field(
        default_factory=lambda: dict(DEFAULT_RUNG_QUALITY)
    )

    def cost_of(self, rung: RenderRung) -> float:
        """Video-seconds the rung spends (only the top rung is non-zero)."""
        return self.video_seconds if rung is RenderRung.FULL_VIDEO else 0.0

    def quality_of(self, rung: RenderRung) -> float:
        """Importance-scaled delivered quality of a rung for this shot."""
        base = self.rung_quality.get(rung, DEFAULT_RUNG_QUALITY[rung])
        return max(base, 0.0) * max(self.importance, 0.0)


@dataclass(frozen=True, slots=True)
class ShotAssignment:
    """The optimizer's pick for one shot."""

    shot_id: str
    rung: RenderRung
    video_seconds: float
    quality: float

    def as_dict(self) -> dict[str, object]:
        return {
            "shot_id": self.shot_id,
            "rung": self.rung.value,
            "video_seconds": round(self.video_seconds, 3),
            "quality": round(self.quality, 4),
        }


@dataclass(frozen=True, slots=True)
class OptimizationPlan:
    """The full optimizer result over a set of shots under a budget."""

    assignments: tuple[ShotAssignment, ...]
    total_quality: float
    total_video_seconds: float
    budget_s: float
    method: str

    @property
    def full_video_count(self) -> int:
        return sum(1 for a in self.assignments if a.rung is RenderRung.FULL_VIDEO)

    @property
    def headroom_s(self) -> float:
        return self.budget_s - self.total_video_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "total_quality": round(self.total_quality, 4),
            "total_video_seconds": round(self.total_video_seconds, 3),
            "budget_s": round(self.budget_s, 3),
            "full_video_count": self.full_video_count,
            "headroom_s": round(self.headroom_s, 3),
            "assignments": [a.as_dict() for a in self.assignments],
        }


def _cheapest_floor(option: ShotOption, *, min_quality: float) -> RenderRung:
    """The cheapest rung meeting ``min_quality`` (or the cheapest of all)."""
    eligible = [r for r in RenderRung if option.quality_of(r) >= min_quality]
    pool = eligible or list(RenderRung)
    # Cheapest = highest rank among the eligible (cost is 0 for all but full video).
    return max(pool, key=lambda r: (r is not RenderRung.FULL_VIDEO, r.rank))


def optimize_greedy(
    options: list[ShotOption],
    *,
    budget_s: float,
    min_quality: float = 0.0,
) -> OptimizationPlan:
    """Greedy marginal-quality-per-second allocation of the budget.

    Start every shot on its cheapest budget-respecting floor rung, then spend the
    remaining budget upgrading shots to **full video** in order of *quality gained
    per video-second* (the steepest bang-for-buck first). Fast, monotone, and a
    good heuristic; :func:`optimize` uses the exact DP and falls back to this.
    """
    floors: dict[str, RenderRung] = {
        o.shot_id: _cheapest_floor(o, min_quality=min_quality) for o in options
    }
    chosen: dict[str, RenderRung] = dict(floors)
    spent = 0.0

    # Candidate upgrades to full video, by marginal quality per second (desc).
    upgrades: list[tuple[float, float, ShotOption]] = []
    for o in options:
        cost = o.cost_of(RenderRung.FULL_VIDEO)
        if cost <= 0.0:
            continue
        gain = o.quality_of(RenderRung.FULL_VIDEO) - o.quality_of(floors[o.shot_id])
        if gain <= 0.0:
            continue
        upgrades.append((gain / cost, cost, o))
    upgrades.sort(key=lambda t: t[0], reverse=True)

    for _ratio, cost, o in upgrades:
        if spent + cost <= budget_s + 1e-9:
            chosen[o.shot_id] = RenderRung.FULL_VIDEO
            spent += cost

    return _plan_from_choices(options, chosen, budget_s=budget_s, method="greedy")


def optimize(
    options: list[ShotOption],
    *,
    budget_s: float,
    min_quality: float = 0.0,
    quantum_s: float = 0.1,
) -> OptimizationPlan:
    """Maximize total delivered quality under ``budget_s`` (exact DP).

    Each shot picks exactly one rung. Only full video has a cost, so the decision
    per shot is binary (full video vs. its cheapest floor), making this a 0/1
    knapsack: maximize Σ gain(full) for the subset of shots upgraded to full video
    whose Σ cost ≤ budget. Costs are quantized to ``quantum_s`` so the DP table is
    small (the video budget is ~1,650s; at 0.1s that is ~16,500 cells per shot,
    and only shots with a positive upgrade gain participate).

    Falls back to :func:`optimize_greedy` when there is nothing to optimize (no
    budget, no shots, or an absurd quantum) so the result is always well-formed.
    """
    if not options or budget_s <= 0.0 or quantum_s <= 0.0:
        return optimize_greedy(options, budget_s=budget_s, min_quality=min_quality)

    floors: dict[str, RenderRung] = {
        o.shot_id: _cheapest_floor(o, min_quality=min_quality) for o in options
    }
    # Items = shots with a positive full-video upgrade gain (others stay at floor).
    items: list[tuple[ShotOption, int, float]] = []
    for o in options:
        cost = o.cost_of(RenderRung.FULL_VIDEO)
        gain = o.quality_of(RenderRung.FULL_VIDEO) - o.quality_of(floors[o.shot_id])
        if cost > 0.0 and gain > 0.0:
            units = max(1, round(cost / quantum_s))
            items.append((o, units, gain))

    cap_units = int(budget_s / quantum_s)
    if not items or cap_units <= 0:
        return optimize_greedy(options, budget_s=budget_s, min_quality=min_quality)

    # 1-D knapsack DP over quantized budget units; track the chosen set via a
    # per-capacity bitset-free reconstruction using a parent table.
    best = [0.0] * (cap_units + 1)
    take: list[list[bool]] = [[False] * (cap_units + 1) for _ in items]
    for idx, (_o, units, gain) in enumerate(items):
        for cap in range(cap_units, units - 1, -1):
            cand = best[cap - units] + gain
            if cand > best[cap] + 1e-12:
                best[cap] = cand
                take[idx][cap] = True

    # Reconstruct which shots were upgraded.
    chosen: dict[str, RenderRung] = dict(floors)
    cap = max(range(cap_units + 1), key=lambda c: best[c])
    for idx in range(len(items) - 1, -1, -1):
        o, units, _gain = items[idx]
        if take[idx][cap]:
            chosen[o.shot_id] = RenderRung.FULL_VIDEO
            cap -= units

    return _plan_from_choices(options, chosen, budget_s=budget_s, method="knapsack")


def _plan_from_choices(
    options: list[ShotOption],
    chosen: dict[str, RenderRung],
    *,
    budget_s: float,
    method: str,
) -> OptimizationPlan:
    assignments: list[ShotAssignment] = []
    total_q = 0.0
    total_v = 0.0
    for o in options:
        rung = chosen[o.shot_id]
        cost = o.cost_of(rung)
        quality = o.quality_of(rung)
        assignments.append(
            ShotAssignment(
                shot_id=o.shot_id, rung=rung, video_seconds=cost, quality=quality
            )
        )
        total_q += quality
        total_v += cost
    return OptimizationPlan(
        assignments=tuple(assignments),
        total_quality=total_q,
        total_video_seconds=total_v,
        budget_s=budget_s,
        method=method,
    )


__all__ = [
    "DEFAULT_RUNG_QUALITY",
    "OptimizationPlan",
    "RenderRung",
    "ShotAssignment",
    "ShotOption",
    "optimize",
    "optimize_greedy",
]
