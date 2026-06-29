"""Property tests for the §4.6/§11.1 budget-optimal promotion knapsack.

``optimize_promotions`` is the near-budget-floor decision: pick the affordable
subset of promotable shots that maximises value (imminence × dwell) without
exceeding the remaining video-seconds. The properties pin the **spend invariant**
(the optimiser can never reserve more than it was handed — the budget-safety
guarantee), the subset/affordability laws, the reading-order output contract, and
— the strongest — **DP optimality** cross-checked against a brute-force oracle on
small inputs (over the *same* quantised value model the DP uses).
"""

from __future__ import annotations

import itertools
import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.scheduler.optimizer import (
    WEIGHT_QUANTUM_S,
    Candidate,
    build_candidate,
    optimize_promotions,
    shot_value,
)
from app.verification.properties.strategies import candidate_runs

budgets = st.floats(min_value=0.0, max_value=40.0, allow_nan=False)
horizons = st.floats(min_value=1.0, max_value=120.0, allow_nan=False)


# --------------------------------------------------------------------------- #
# shot_value — the value model
# --------------------------------------------------------------------------- #


@given(
    st.floats(min_value=0.0, max_value=500.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=60.0, allow_nan=False),
    st.floats(min_value=0.5, max_value=15.0, allow_nan=False),
    horizons,
)
def test_shot_value_is_positive_and_bounded(
    eta: float, dwell: float, duration: float, horizon: float
) -> None:
    """Value is in ``(0, 3]`` — imminence ∈ (0,1] × dwell bonus ∈ [1,3]."""
    c = Candidate(shot_id="s", est_duration_s=duration, eta_s=eta, dwell_s=dwell)
    v = shot_value(c, commit_horizon_s=horizon)
    assert 0.0 < v <= 3.0 + 1e-9


@given(
    st.floats(min_value=0.0, max_value=200.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=200.0, allow_nan=False),
    st.floats(min_value=0.5, max_value=10.0, allow_nan=False),
    horizons,
)
def test_value_is_monotone_decreasing_in_eta(
    eta_a: float, eta_b: float, duration: float, horizon: float
) -> None:
    """A nearer shot (smaller ETA) is worth at least as much (all else equal)."""
    near = Candidate("s", est_duration_s=duration, eta_s=min(eta_a, eta_b), dwell_s=1.0)
    far = Candidate("s", est_duration_s=duration, eta_s=max(eta_a, eta_b), dwell_s=1.0)
    assert shot_value(near, commit_horizon_s=horizon) >= shot_value(
        far, commit_horizon_s=horizon
    )


@given(
    st.floats(min_value=50.0, max_value=200.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=200.0, allow_nan=False),
    st.floats(min_value=0.5, max_value=10.0, allow_nan=False),
    horizons,
)
def test_value_is_monotone_increasing_in_dwell(
    dwell_a: float, dwell_b: float, duration: float, horizon: float
) -> None:
    """A more-dwelt-on shot is worth at least as much (all else equal)."""
    less = Candidate("s", est_duration_s=duration, eta_s=5.0, dwell_s=min(dwell_a, dwell_b))
    more = Candidate("s", est_duration_s=duration, eta_s=5.0, dwell_s=max(dwell_a, dwell_b))
    assert shot_value(more, commit_horizon_s=horizon) >= shot_value(
        less, commit_horizon_s=horizon
    )


# --------------------------------------------------------------------------- #
# optimize_promotions — the spend invariant + structural laws
# --------------------------------------------------------------------------- #


@given(candidate_runs(), budgets, horizons)
def test_selection_never_exceeds_budget(
    cands: list[Candidate], budget: float, horizon: float
) -> None:
    """THE spend invariant: total reserved video-seconds ≤ remaining (§ critical)."""
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    assert sel.total_video_s <= budget + 1e-6


@given(candidate_runs(), budgets, horizons)
def test_chosen_is_a_subset_of_affordable_candidates(
    cands: list[Candidate], budget: float, horizon: float
) -> None:
    """Every chosen shot was an input candidate that individually fit the budget."""
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    by_id = {c.shot_id: c for c in cands}
    for c in sel.chosen:
        assert c.shot_id in by_id
        assert 0.0 < c.est_duration_s <= budget + 1e-9


@given(candidate_runs(), budgets, horizons)
def test_chosen_ids_are_unique(
    cands: list[Candidate], budget: float, horizon: float
) -> None:
    """0/1 knapsack: no shot is promoted twice."""
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    assert len(sel.chosen_ids) == len(set(sel.chosen_ids))


@given(candidate_runs(), budgets, horizons)
def test_output_is_reading_ordered(
    cands: list[Candidate], budget: float, horizon: float
) -> None:
    """The chosen set is returned nearest-ETA-first (the fill loop enqueues so)."""
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    etas = [c.eta_s for c in sel.chosen]
    assert etas == sorted(etas)


@given(candidate_runs(), horizons)
def test_zero_budget_selects_nothing(cands: list[Candidate], horizon: float) -> None:
    """A closed budget gate (≤ 0 remaining) promotes nothing — no spend."""
    sel = optimize_promotions(cands, remaining_video_s=0.0, commit_horizon_s=horizon)
    assert sel.chosen == []
    assert sel.total_video_s == 0.0


@given(candidate_runs(), horizons)
def test_everything_fits_takes_all_affordable(
    cands: list[Candidate], horizon: float
) -> None:
    """When the whole affordable set fits, all of it is promoted (greedy == optimal)."""
    affordable = [c for c in cands if c.est_duration_s > 0.0]
    budget = sum(c.est_duration_s for c in affordable) + 1.0
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    assert set(sel.chosen_ids) == {c.shot_id for c in affordable}


@given(candidate_runs(), budgets, budgets, horizons)
def test_value_is_monotone_in_budget(
    cands: list[Candidate], b1: float, b2: float, horizon: float
) -> None:
    """A larger budget never yields *less* total value (more room can't hurt)."""
    lo, hi = min(b1, b2), max(b1, b2)
    sel_lo = optimize_promotions(cands, remaining_video_s=lo, commit_horizon_s=horizon)
    sel_hi = optimize_promotions(cands, remaining_video_s=hi, commit_horizon_s=horizon)
    assert sel_hi.total_value >= sel_lo.total_value - 1e-6


# --------------------------------------------------------------------------- #
# DP optimality — cross-checked against brute force on small inputs
# --------------------------------------------------------------------------- #


def _quantised_weight(c: Candidate) -> int:
    return max(1, int(math.ceil(c.est_duration_s / WEIGHT_QUANTUM_S)))


def _brute_force_best_value(
    cands: list[Candidate], capacity: float, horizon: float
) -> float:
    """The optimal total value over the SAME quantised weights the DP uses.

    Enumerates every subset whose quantised weight fits the quantised capacity and
    returns the max total ``shot_value`` — the ground-truth oracle for the DP.
    """
    affordable = [c for c in cands if 0.0 < c.est_duration_s <= capacity]
    cap_units = int(math.floor(capacity / WEIGHT_QUANTUM_S))
    best = 0.0
    for r in range(len(affordable) + 1):
        for combo in itertools.combinations(affordable, r):
            if sum(_quantised_weight(c) for c in combo) <= cap_units:
                value = sum(shot_value(c, commit_horizon_s=horizon) for c in combo)
                best = max(best, value)
    return best


@given(candidate_runs(max_size=6), budgets, horizons)
@settings(max_examples=200)
def test_dp_selection_is_value_optimal(
    cands: list[Candidate], budget: float, horizon: float
) -> None:
    """The optimiser's chosen value equals the brute-force optimum (DP correctness).

    Restricted to ≤6 candidates so brute force (2^n) is cheap; the value is compared
    against the optimum over the identical 0.5s-quantised weight model.
    """
    # Only meaningful when the DP path is actually exercised (over-subscribed budget).
    affordable = [c for c in cands if 0.0 < c.est_duration_s <= budget]
    assume(budget > 0.0 and affordable)
    sel = optimize_promotions(cands, remaining_video_s=budget, commit_horizon_s=horizon)
    optimal = _brute_force_best_value(cands, budget, horizon)
    assert sel.total_value <= optimal + 1e-6
    assert sel.total_value >= optimal - 1e-6


# --------------------------------------------------------------------------- #
# build_candidate — the §4.3 bridge
# --------------------------------------------------------------------------- #


@given(
    st.integers(min_value=0, max_value=1_000_000),
    st.integers(min_value=0, max_value=1_000_000),
    st.floats(min_value=-50.0, max_value=50.0, allow_nan=False),
    st.floats(min_value=0.5, max_value=15.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_build_candidate_eta_and_dwell_are_nonnegative(
    start: int, focus: int, velocity: float, duration: float, dwell_ms: float
) -> None:
    """ETA and dwell are clamped non-negative; duration passes through verbatim."""
    c = build_candidate(
        shot_id="s",
        word_index_start=start,
        focus_word=focus,
        velocity_wps=velocity,
        est_duration_s=duration,
        dwell_ms=dwell_ms,
    )
    assert c.eta_s >= 0.0
    assert c.dwell_s >= 0.0
    assert c.est_duration_s == duration
