"""Budget-optimal promotion tests (kinora.md §4.6/§11.1/§12.2) — pure, no infra.

Pin the knapsack: a plentiful budget takes everything (greedy == optimal, matching
today's fill); a scarce budget prefers imminent + dwelt-on shots and never exceeds
capacity; a closed gate (remaining 0) selects nothing; the value model is monotone.
"""

from __future__ import annotations

from app.scheduler.optimizer import (
    Candidate,
    build_candidate,
    optimize_promotions,
    shot_value,
)


def _c(shot_id: str, dur: float, eta: float, dwell: float = 0.0) -> Candidate:
    return Candidate(shot_id=shot_id, est_duration_s=dur, eta_s=eta, dwell_s=dwell)


# --- value model monotonicity (§4.6) --------------------------------------- #


def test_value_decreases_with_eta() -> None:
    near = shot_value(_c("a", 5.0, 5.0), commit_horizon_s=45.0)
    far = shot_value(_c("a", 5.0, 40.0), commit_horizon_s=45.0)
    assert near > far


def test_value_increases_with_dwell() -> None:
    plain = shot_value(_c("a", 5.0, 10.0, dwell=0.0), commit_horizon_s=45.0)
    dwelt = shot_value(_c("a", 5.0, 10.0, dwell=10.0), commit_horizon_s=45.0)
    assert dwelt > plain


# --- plentiful budget: take everything (greedy == optimal) ----------------- #


def test_plentiful_budget_takes_all_in_reading_order() -> None:
    cands = [_c("s3", 5.0, 30.0), _c("s1", 5.0, 10.0), _c("s2", 5.0, 20.0)]
    sel = optimize_promotions(cands, remaining_video_s=1000.0, commit_horizon_s=45.0)
    assert sel.chosen_ids == ["s1", "s2", "s3"]  # nearest-ETA first
    assert sel.total_video_s == 15.0


# --- scarce budget: knapsack chooses the most valuable affordable set ------- #


def test_scarce_budget_prefers_imminent_shots() -> None:
    # Capacity for exactly two 5s shots. Three candidates; nearest two win.
    cands = [_c("near", 5.0, 5.0), _c("mid", 5.0, 25.0), _c("far", 5.0, 44.0)]
    sel = optimize_promotions(cands, remaining_video_s=10.0, commit_horizon_s=45.0)
    assert set(sel.chosen_ids) == {"near", "mid"}
    assert sel.total_video_s <= 10.0


def test_scarce_budget_prefers_two_short_over_one_long() -> None:
    # 10s budget: one 10s far shot vs two 5s near shots. Near pair wins on value.
    cands = [_c("long_far", 10.0, 40.0), _c("short_a", 5.0, 5.0), _c("short_b", 5.0, 8.0)]
    sel = optimize_promotions(cands, remaining_video_s=10.0, commit_horizon_s=45.0)
    assert set(sel.chosen_ids) == {"short_a", "short_b"}


def test_never_exceeds_capacity() -> None:
    cands = [_c(f"s{i}", 5.0, float(i * 5)) for i in range(10)]
    sel = optimize_promotions(cands, remaining_video_s=12.0, commit_horizon_s=45.0)
    assert sel.total_video_s <= 12.0


# --- closed gate / edge cases ---------------------------------------------- #


def test_closed_gate_selects_nothing() -> None:
    cands = [_c("s1", 5.0, 5.0)]
    sel = optimize_promotions(cands, remaining_video_s=0.0, commit_horizon_s=45.0)
    assert sel.chosen == []
    assert sel.total_video_s == 0.0


def test_unaffordable_candidates_are_skipped() -> None:
    # Every shot is bigger than the budget → nothing selectable.
    cands = [_c("big", 20.0, 5.0)]
    sel = optimize_promotions(cands, remaining_video_s=10.0, commit_horizon_s=45.0)
    assert sel.chosen == []


def test_large_candidate_set_stays_budget_safe() -> None:
    # Beyond the exact-DP cap → density greedy, still never over capacity.
    cands = [_c(f"s{i}", 5.0, float(i)) for i in range(200)]
    sel = optimize_promotions(cands, remaining_video_s=33.0, commit_horizon_s=45.0)
    assert sel.total_video_s <= 33.0
    assert len(sel.chosen) == 6  # floor(33 / 5)


# --- build_candidate bridges the §4.3 ETA math ----------------------------- #


def test_build_candidate_computes_eta_and_dwell() -> None:
    c = build_candidate(
        shot_id="s1",
        word_index_start=400,
        focus_word=0,
        velocity_wps=4.0,
        est_duration_s=5.0,
        dwell_ms=2000.0,
    )
    assert c.eta_s == 100.0  # (400 - 0) / 4
    assert c.dwell_s == 2.0
    assert c.est_duration_s == 5.0
