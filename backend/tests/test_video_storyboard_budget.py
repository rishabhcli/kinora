"""Unit tests for shot-count + duration budgeting (deterministic, no network)."""

from __future__ import annotations

from app.agents.contracts import SceneTempo
from app.video.storyboard import (
    ShotCoverage,
    ShotDurationInput,
    StoryboardBudget,
    allocate_durations,
    allocate_shot_counts,
)

# --- shot-count budget ----------------------------------------------------- #


def test_every_beat_keeps_its_head_shot_under_pressure() -> None:
    # Three beats, each proposing 3 candidates, but a ceiling of 3 shots total.
    candidates = [
        (SceneTempo.SCENE, [ShotCoverage.MASTER, ShotCoverage.REACTION, ShotCoverage.INSERT]),
        (SceneTempo.SCENE, [ShotCoverage.MASTER, ShotCoverage.REACTION, ShotCoverage.INSERT]),
        (SceneTempo.SCENE, [ShotCoverage.MASTER, ShotCoverage.REACTION, ShotCoverage.INSERT]),
    ]
    allocs = allocate_shot_counts(candidates, StoryboardBudget(max_shots=3))
    # Exactly the ceiling, one head each, no beat starved.
    assert sum(len(a.coverage) for a in allocs) == 3
    assert all(len(a.coverage) == 1 for a in allocs)
    assert all(a.coverage[0] is ShotCoverage.MASTER for a in allocs)


def test_extra_budget_favours_denser_tempo() -> None:
    # A SCENE (density 1.0) vs a SUMMARY (density 0.4); 1 spare shot to hand out.
    candidates = [
        (SceneTempo.SCENE, [ShotCoverage.MASTER, ShotCoverage.REACTION]),
        (SceneTempo.SUMMARY, [ShotCoverage.MASTER, ShotCoverage.INSERT]),
    ]
    allocs = allocate_shot_counts(candidates, StoryboardBudget(max_shots=3))
    by_index = {a.beat_index: len(a.coverage) for a in allocs}
    assert by_index[0] == 2  # the dense SCENE earned the spare shot
    assert by_index[1] == 1


def test_never_exceeds_candidate_count() -> None:
    candidates = [(SceneTempo.SCENE, [ShotCoverage.MASTER])]
    allocs = allocate_shot_counts(candidates, StoryboardBudget(max_shots=10))
    assert len(allocs[0].coverage) == 1  # only one candidate exists


def test_ceiling_caps_total_when_candidates_abound() -> None:
    candidates = [
        (SceneTempo.SCENE, [ShotCoverage.MASTER, ShotCoverage.REACTION, ShotCoverage.INSERT])
        for _ in range(5)
    ]
    allocs = allocate_shot_counts(candidates, StoryboardBudget(max_shots=6))
    assert sum(len(a.coverage) for a in allocs) == 6


# --- duration budget ------------------------------------------------------- #


def test_durations_sum_to_target_within_band() -> None:
    shots = [ShotDurationInput(tempo=SceneTempo.SCENE, words=20) for _ in range(5)]
    budget = StoryboardBudget(target_total_s=25.0, min_shot_s=3.0, max_shot_s=8.0)
    durs = allocate_durations(shots, budget)
    assert abs(sum(durs) - 25.0) < 0.2  # within rounding
    assert all(3.0 <= d <= 8.0 for d in durs)


def test_durations_respect_band_when_target_infeasible_high() -> None:
    # Target 100s over 5 shots needs 20s each — clamps to the 8s ceiling.
    shots = [ShotDurationInput(tempo=SceneTempo.SCENE, words=20) for _ in range(5)]
    budget = StoryboardBudget(target_total_s=100.0, tolerance_s=2.0, max_shot_s=8.0)
    durs = allocate_durations(shots, budget)
    assert all(d <= 8.0 for d in durs)
    assert sum(durs) <= 40.5  # band wins; far under target


def test_durations_respect_band_when_target_infeasible_low() -> None:
    # Target 4s over 5 shots needs 0.8s each — clamps up to the 3s floor.
    shots = [ShotDurationInput(tempo=SceneTempo.SCENE, words=20) for _ in range(5)]
    budget = StoryboardBudget(target_total_s=4.0, min_shot_s=3.0, max_shot_s=8.0)
    durs = allocate_durations(shots, budget)
    assert all(d >= 3.0 for d in durs)


def test_longer_narration_earns_more_screen_time() -> None:
    shots = [
        ShotDurationInput(tempo=SceneTempo.SCENE, words=10),
        ShotDurationInput(tempo=SceneTempo.SCENE, words=50),
    ]
    budget = StoryboardBudget(target_total_s=11.0, min_shot_s=3.0, max_shot_s=8.0)
    durs = allocate_durations(shots, budget)
    assert durs[1] >= durs[0]


def test_empty_shots_returns_empty() -> None:
    assert allocate_durations([], StoryboardBudget()) == []
