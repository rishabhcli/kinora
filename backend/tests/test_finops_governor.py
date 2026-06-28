"""The budget governor brain (kinora.md §11.1, §4.6). Pure — no infra."""

from __future__ import annotations

import pytest

from app.finops.forecast import ReadingTrajectory
from app.finops.governor import Recommendation, govern, rung_priority_order
from app.finops.optimizer import RenderRung, ShotOption
from app.finops.tiers import BudgetScopeKind, BudgetTierPolicy, TieredCap


def _policy(
    *, ceiling: float = 1000.0, per_session: float = 100.0, per_scene: float = 50.0
) -> BudgetTierPolicy:
    return BudgetTierPolicy(
        global_cap=TieredCap(BudgetScopeKind.GLOBAL, ceiling),
        tenant_cap=TieredCap(BudgetScopeKind.TENANT, float("inf")),
        session_cap=TieredCap(BudgetScopeKind.SESSION, per_session),
        scene_cap=TieredCap(BudgetScopeKind.SCENE, per_scene),
    )


def _traj(velocity_wps: float = 4.0, words_remaining: int = 4000) -> ReadingTrajectory:
    return ReadingTrajectory(velocity_wps=velocity_wps, words_remaining=words_remaining)


def _shots(n: int, seconds: float = 5.0) -> list[ShotOption]:
    return [ShotOption(shot_id=f"s{i}", video_seconds=seconds) for i in range(n)]


def test_promote_when_plenty_of_headroom() -> None:
    decision = govern(
        _policy(),
        used_by_scope={BudgetScopeKind.GLOBAL: 0.0, BudgetScopeKind.SESSION: 0.0},
        trajectory=_traj(),
        upcoming=_shots(3),
        horizon_s=60.0,
    )
    assert decision.recommendation is Recommendation.PROMOTE
    assert decision.plan.full_video_count == 3


def test_optimize_when_soft_cap_crossed() -> None:
    # Session at 95/100 -> soft cap. Should advise OPTIMIZE and constrain the plan
    # to the binding (session) headroom of 5s -> only 1 full-video shot.
    decision = govern(
        _policy(),
        used_by_scope={BudgetScopeKind.GLOBAL: 100.0, BudgetScopeKind.SESSION: 95.0},
        trajectory=_traj(),
        upcoming=_shots(3, seconds=5.0),
        horizon_s=60.0,
    )
    assert decision.recommendation is Recommendation.OPTIMIZE
    assert decision.binding is not None
    assert decision.binding.scope is BudgetScopeKind.SESSION
    assert decision.plan.total_video_seconds <= 5.0 + 1e-9


def test_halt_when_hard_cap_reached_rides_the_ladder() -> None:
    decision = govern(
        _policy(),
        used_by_scope={BudgetScopeKind.GLOBAL: 100.0, BudgetScopeKind.SESSION: 100.0},
        trajectory=_traj(),
        upcoming=_shots(3),
        horizon_s=60.0,
    )
    assert decision.recommendation is Recommendation.HALT
    # No full video promoted under HALT — every shot rides the ladder.
    assert decision.plan.full_video_count == 0


def test_optimize_when_forecast_does_not_fit_even_under_soft() -> None:
    # Headroom exists (well under soft) but the forecast forward spend exceeds it.
    policy = _policy(ceiling=1000.0, per_session=1000.0)
    # remaining global headroom 20s; a fast reader forecasts more than that.
    decision = govern(
        policy,
        used_by_scope={BudgetScopeKind.GLOBAL: 980.0},
        trajectory=_traj(velocity_wps=20.0, words_remaining=100_000),
        upcoming=_shots(3),
        horizon_s=600.0,
    )
    assert not decision.forecast.fits
    assert decision.recommendation is Recommendation.OPTIMIZE


def test_decision_as_dict_serializable() -> None:
    decision = govern(
        _policy(),
        used_by_scope={BudgetScopeKind.GLOBAL: 0.0},
        trajectory=_traj(),
        upcoming=_shots(2),
        horizon_s=60.0,
    )
    d = decision.as_dict()
    assert set(d) >= {"recommendation", "worst_level", "statuses", "forecast", "plan"}


def test_rung_priority_order() -> None:
    order = rung_priority_order()
    assert order[0] is RenderRung.FULL_VIDEO
    assert order[-1] is RenderRung.TEXT_AUDIO
    assert [r.rank for r in order] == [0, 1, 2, 3]


def test_binding_headroom_property() -> None:
    decision = govern(
        _policy(),
        used_by_scope={BudgetScopeKind.GLOBAL: 0.0, BudgetScopeKind.SESSION: 90.0},
        trajectory=_traj(),
        upcoming=_shots(1),
        horizon_s=60.0,
    )
    assert decision.binding_headroom_s == pytest.approx(10.0)
