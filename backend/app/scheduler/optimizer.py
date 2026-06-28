"""Budget-optimal promotion selection (kinora.md §4.6/§11.1/§12.2).

The §4.6 fill loop is *greedy in reading order*: it promotes the next uncommitted
shot, then the next, until it hits ``H``, the commit horizon, or the budget. That
is correct and cheap, and when the budget is plentiful (the common case) greedy
== optimal — every affordable shot ahead gets promoted anyway.

But near the **budget floor** (§11.1, ``budget_low``) the order matters. With only
a few video-seconds left, spending them on a long shot the reader will reach in
40s instead of two short shots they'll reach in 10s is a worse use of the scarce
currency. This module makes that choice **optimal**: given the affordable
candidates and the remaining video-seconds, pick the subset that maximises total
*value*, where value rewards imminence (low ETA) and reader dwell (a shot the
reader will linger on is worth more committed video than one they'll blow past).

This is a 0/1 knapsack:

* **weight** = a shot's ``est_duration_s`` (the video-seconds it would reserve);
* **capacity** = remaining video-seconds (the budget the §4.5 fill may spend);
* **value** = ``f(eta, dwell, est_duration)`` — higher for sooner, dwelt-on shots.

Because durations are small multiples of a base shot length, the weights bucket
into a small integer grid, so an exact dynamic-programming knapsack is cheap; for
the plentiful-budget case the solver short-circuits to "take everything" (greedy),
matching today's behaviour exactly.

**Spend invariant.** The optimiser only *chooses among candidates the budget gate
already permits* and never returns a set whose total weight exceeds the capacity
it was handed. It cannot raise the ceiling, promote past ``can_render_live()``, or
reserve more than `remaining`. With the live gate off, ``remaining`` reflects a
closed gate upstream and the optimiser simply selects nothing to spend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.scheduler.zones import DEFAULT_VELOCITY_WPS

#: Quantisation grid for knapsack weights (seconds). Shot durations are small
#: multiples of a base length, so 0.5s buckets keep the DP table tiny and exact.
WEIGHT_QUANTUM_S = 0.5
#: Above this many candidates the exact DP is skipped for a value-density greedy
#: (still budget-safe); keeps the optimiser O(n log n) on pathological inputs.
EXACT_DP_CANDIDATE_CAP = 64


@dataclass(frozen=True, slots=True)
class Candidate:
    """A promotable shot the optimiser may select (a thin view of a shot)."""

    shot_id: str
    #: Video-seconds this promotion would reserve (the knapsack weight).
    est_duration_s: float
    #: Reading-seconds until the reader reaches the shot (lower = more urgent).
    eta_s: float
    #: Estimated reader dwell on the shot, seconds (higher = worth more video).
    dwell_s: float = 0.0


@dataclass(frozen=True, slots=True)
class Selection:
    """The optimiser's choice: which candidates to promote, in priority order."""

    chosen: list[Candidate]
    total_video_s: float
    total_value: float

    @property
    def chosen_ids(self) -> list[str]:
        return [c.shot_id for c in self.chosen]


def shot_value(candidate: Candidate, *, commit_horizon_s: float) -> float:
    """The §4.6 value of committing ``candidate`` to full video (higher = better).

    Rewards **imminence** — a shot the reader reaches sooner is worth more, since
    a buffer stall hurts most at the playhead — via a decay in ETA, and rewards
    **dwell** — a shot the reader lingers on returns more value per video-second.
    Normalised so a 0-ETA, baseline-dwell shot scores ~1.0; monotonic decreasing
    in ETA, monotonic increasing in dwell.
    """
    # Imminence: 1.0 at the playhead, decaying to ~0.37 at the commit horizon.
    horizon = max(1.0, commit_horizon_s)
    imminence = math.exp(-max(0.0, candidate.eta_s) / horizon)
    # Dwell bonus: a shot the reader dwells on longer than the clip is worth more.
    base_dwell = max(1.0, candidate.est_duration_s)
    dwell_bonus = 1.0 + min(2.0, max(0.0, candidate.dwell_s) / base_dwell)
    return imminence * dwell_bonus


def optimize_promotions(
    candidates: list[Candidate],
    *,
    remaining_video_s: float,
    commit_horizon_s: float = 45.0,
) -> Selection:
    """Pick the value-maximising affordable subset of ``candidates`` (§4.6/§11.1).

    A 0/1 knapsack: maximise total :func:`shot_value` subject to total
    ``est_duration_s <= remaining_video_s``. Returns the chosen set in **reading
    order** (by ETA) so the fill loop enqueues nearest-first.

    Fast paths:
      * ``remaining_video_s <= 0`` → choose nothing (a closed budget gate).
      * everything fits → take all (greedy == optimal; today's behaviour).
      * ``> EXACT_DP_CANDIDATE_CAP`` candidates → value-density greedy (budget-safe).
    """
    affordable = [
        c
        for c in candidates
        if 0.0 < c.est_duration_s <= remaining_video_s
    ]
    if remaining_video_s <= 0.0 or not affordable:
        return Selection(chosen=[], total_video_s=0.0, total_value=0.0)

    total_weight = sum(c.est_duration_s for c in affordable)
    if total_weight <= remaining_video_s:
        # Everything fits: greedy is optimal — exactly the §4.5 fill today.
        return _selection(affordable, commit_horizon_s)

    if len(affordable) > EXACT_DP_CANDIDATE_CAP:
        return _greedy_by_density(affordable, remaining_video_s, commit_horizon_s)

    return _knapsack_dp(affordable, remaining_video_s, commit_horizon_s)


def _selection(chosen: list[Candidate], commit_horizon_s: float) -> Selection:
    """Build a :class:`Selection` from a chosen set, ordered nearest-ETA first."""
    ordered = sorted(chosen, key=lambda c: c.eta_s)
    return Selection(
        chosen=ordered,
        total_video_s=round(sum(c.est_duration_s for c in ordered), 6),
        total_value=round(
            sum(shot_value(c, commit_horizon_s=commit_horizon_s) for c in ordered), 6
        ),
    )


def _greedy_by_density(
    candidates: list[Candidate], capacity: float, commit_horizon_s: float
) -> Selection:
    """Greedy by value-per-second (a budget-safe heuristic for huge candidate sets)."""
    ranked = sorted(
        candidates,
        key=lambda c: shot_value(c, commit_horizon_s=commit_horizon_s) / c.est_duration_s,
        reverse=True,
    )
    chosen: list[Candidate] = []
    used = 0.0
    for c in ranked:
        if used + c.est_duration_s <= capacity:
            chosen.append(c)
            used += c.est_duration_s
    return _selection(chosen, commit_horizon_s)


def _knapsack_dp(
    candidates: list[Candidate], capacity: float, commit_horizon_s: float
) -> Selection:
    """Exact 0/1 knapsack over quantised weights (maximise total value)."""
    cap_units = int(math.floor(capacity / WEIGHT_QUANTUM_S))
    n = len(candidates)
    weights = [max(1, int(math.ceil(c.est_duration_s / WEIGHT_QUANTUM_S))) for c in candidates]
    values = [shot_value(c, commit_horizon_s=commit_horizon_s) for c in candidates]

    # dp[w] = best value achievable with total weight <= w; keep a back-pointer
    # set per cell to reconstruct the chosen items.
    dp = [0.0] * (cap_units + 1)
    pick: list[list[int]] = [[] for _ in range(cap_units + 1)]
    for i in range(n):
        wi, vi = weights[i], values[i]
        # iterate capacity descending for the 0/1 (each item used at most once)
        for w in range(cap_units, wi - 1, -1):
            cand = dp[w - wi] + vi
            if cand > dp[w]:
                dp[w] = cand
                pick[w] = pick[w - wi] + [i]

    best_w = max(range(cap_units + 1), key=lambda w: dp[w])
    chosen = [candidates[i] for i in pick[best_w]]
    return _selection(chosen, commit_horizon_s)


def build_candidate(
    *,
    shot_id: str,
    word_index_start: int,
    focus_word: int,
    velocity_wps: float,
    est_duration_s: float,
    dwell_ms: float = 0.0,
) -> Candidate:
    """Build a :class:`Candidate` from raw shot/session fields (§4.3 ETA math).

    Bridges the scheduler's per-shot fields to the optimiser's value model: ETA is
    the §4.3 ``(start − w) / v`` and dwell is the prediction model's per-position
    dwell projected onto this shot.
    """
    v = max(0.1, abs(velocity_wps) or DEFAULT_VELOCITY_WPS)
    eta = max(0.0, (word_index_start - focus_word) / v)
    return Candidate(
        shot_id=shot_id,
        est_duration_s=est_duration_s,
        eta_s=eta,
        dwell_s=max(0.0, dwell_ms) / 1000.0,
    )


__all__ = [
    "EXACT_DP_CANDIDATE_CAP",
    "WEIGHT_QUANTUM_S",
    "Candidate",
    "Selection",
    "build_candidate",
    "optimize_promotions",
    "shot_value",
]
