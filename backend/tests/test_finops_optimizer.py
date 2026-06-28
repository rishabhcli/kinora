"""Quality↔budget render-mode optimizer (kinora.md §12.4, §11.1). Pure — no infra."""

from __future__ import annotations

import pytest

from app.finops.optimizer import (
    DEFAULT_RUNG_QUALITY,
    RenderRung,
    ShotOption,
    optimize,
    optimize_greedy,
)


def _shots(n: int, *, seconds: float = 5.0, importance: float = 1.0) -> list[ShotOption]:
    return [
        ShotOption(shot_id=f"s{i}", video_seconds=seconds, importance=importance)
        for i in range(n)
    ]


def test_rung_rank_order() -> None:
    ranks = [r.rank for r in (
        RenderRung.FULL_VIDEO,
        RenderRung.KEYFRAME_KENBURNS,
        RenderRung.ILLUSTRATION_KENBURNS,
        RenderRung.TEXT_AUDIO,
    )]
    assert ranks == [0, 1, 2, 3]


def test_only_full_video_costs_budget() -> None:
    o = ShotOption(shot_id="s", video_seconds=5.0)
    assert o.cost_of(RenderRung.FULL_VIDEO) == 5.0
    for rung in (
        RenderRung.KEYFRAME_KENBURNS,
        RenderRung.ILLUSTRATION_KENBURNS,
        RenderRung.TEXT_AUDIO,
    ):
        assert o.cost_of(rung) == 0.0


def test_importance_scales_quality() -> None:
    o = ShotOption(shot_id="s", video_seconds=5.0, importance=2.0)
    assert o.quality_of(RenderRung.FULL_VIDEO) == pytest.approx(
        DEFAULT_RUNG_QUALITY[RenderRung.FULL_VIDEO] * 2.0
    )


def test_unlimited_budget_promotes_everyone_to_full_video() -> None:
    shots = _shots(5)
    plan = optimize(shots, budget_s=1000.0)
    assert plan.full_video_count == 5
    assert plan.total_video_seconds == pytest.approx(25.0)
    assert all(a.rung is RenderRung.FULL_VIDEO for a in plan.assignments)


def test_zero_budget_degrades_everyone() -> None:
    shots = _shots(5)
    plan = optimize(shots, budget_s=0.0)
    assert plan.full_video_count == 0
    assert plan.total_video_seconds == 0.0


def test_optimizer_respects_budget_cap() -> None:
    shots = _shots(10, seconds=5.0)
    plan = optimize(shots, budget_s=12.0)  # room for 2 full-video shots
    assert plan.full_video_count == 2
    assert plan.total_video_seconds <= 12.0 + 1e-9


def test_optimizer_prefers_high_importance_shots() -> None:
    # Two cheap-but-trivial shots and one pivotal shot; budget for exactly one.
    shots = [
        ShotOption(shot_id="trivial_a", video_seconds=5.0, importance=0.1),
        ShotOption(shot_id="trivial_b", video_seconds=5.0, importance=0.1),
        ShotOption(shot_id="pivotal", video_seconds=5.0, importance=5.0),
    ]
    plan = optimize(shots, budget_s=5.0)
    chosen = {a.shot_id: a.rung for a in plan.assignments}
    assert chosen["pivotal"] is RenderRung.FULL_VIDEO
    assert plan.full_video_count == 1


def test_knapsack_beats_or_matches_greedy_on_total_quality() -> None:
    # A case where greedy's per-second ranking can be suboptimal: a big-cost high
    # absolute-quality shot vs. several small efficient ones.
    shots = [
        ShotOption(shot_id="big", video_seconds=7.0, importance=2.0),
        ShotOption(shot_id="a", video_seconds=4.0, importance=1.0),
        ShotOption(shot_id="b", video_seconds=4.0, importance=1.0),
    ]
    budget = 8.0
    exact = optimize(shots, budget_s=budget)
    greedy = optimize_greedy(shots, budget_s=budget)
    assert exact.total_quality >= greedy.total_quality - 1e-9
    assert exact.total_video_seconds <= budget + 1e-9


def test_min_quality_floor_lifts_degraded_rung() -> None:
    shots = _shots(3)
    # With a min-quality floor above text-audio, the floor rung is a higher one.
    plan = optimize(shots, budget_s=0.0, min_quality=0.5)
    # Every degraded shot still meets at least the keyframe rung quality (0.6).
    for a in plan.assignments:
        assert a.rung is not RenderRung.TEXT_AUDIO


def test_plan_as_dict_serializable() -> None:
    plan = optimize(_shots(2), budget_s=5.0)
    d = plan.as_dict()
    assert set(d) >= {"method", "total_quality", "total_video_seconds", "assignments"}
    assert len(d["assignments"]) == 2  # type: ignore[arg-type]


def test_empty_shots_is_well_formed() -> None:
    plan = optimize([], budget_s=100.0)
    assert plan.assignments == ()
    assert plan.total_quality == 0.0
    assert plan.total_video_seconds == 0.0
