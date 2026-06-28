"""Tests for the rollout strategy planners (pure)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.models import RolloutStrategy
from deploy.orchestrator.strategies import (
    BlueGreenStrategy,
    CanaryStep,
    CanaryStrategy,
    RecreateStrategy,
    RolloutPlan,
    RolloutStep,
    strategy_for,
)


def test_blue_green_plan_shape() -> None:
    plan = BlueGreenStrategy().plan()
    assert plan.strategy is RolloutStrategy.BLUE_GREEN
    assert len(plan) == 2
    stage, cutover = plan.steps
    assert stage.is_stage and stage.weight == 0.0 and stage.verify is False
    assert cutover.is_cutover and cutover.weight == 1.0 and cutover.verify is True
    assert plan.final_weight == 1.0


def test_canary_default_ladder() -> None:
    plan = CanaryStrategy().plan()
    assert plan.strategy is RolloutStrategy.CANARY
    weights = [s.weight for s in plan.steps]
    assert weights == [0.05, 0.25, 0.5, 1.0]
    assert all(s.verify for s in plan.steps)
    assert plan.steps[0].label == "canary-5pct"


def test_canary_custom_weights() -> None:
    plan = CanaryStrategy(weights=(0.1, 1.0)).plan()
    assert [s.weight for s in plan.steps] == [0.1, 1.0]


def test_canary_from_steps_carries_labels() -> None:
    strat = CanaryStrategy.from_steps([CanaryStep(0.2, "small"), CanaryStep(1.0, "full")])
    plan = strat.plan()
    assert [s.label for s in plan.steps] == ["small", "full"]


def test_canary_weights_must_end_at_one() -> None:
    with pytest.raises(ValueError):
        CanaryStrategy(weights=(0.1, 0.5))


def test_canary_weights_must_increase() -> None:
    with pytest.raises(ValueError):
        CanaryStrategy(weights=(0.5, 0.5, 1.0))
    with pytest.raises(ValueError):
        CanaryStrategy(weights=(0.5, 0.2, 1.0))


def test_canary_weights_in_range() -> None:
    with pytest.raises(ValueError):
        CanaryStrategy(weights=(0.0, 1.0))  # 0 not allowed (must be >0)
    with pytest.raises(ValueError):
        CanaryStrategy(weights=(1.5,))


def test_recreate_plan() -> None:
    plan = RecreateStrategy().plan()
    assert plan.strategy is RolloutStrategy.RECREATE
    assert len(plan) == 1
    assert plan.steps[0].weight == 1.0


def test_rollout_step_weight_bounds() -> None:
    with pytest.raises(ValueError):
        RolloutStep(weight=1.5, label="bad")
    with pytest.raises(ValueError):
        RolloutStep(weight=-0.1, label="bad")


def test_rollout_plan_requires_final_full_weight() -> None:
    with pytest.raises(ValueError):
        RolloutPlan(strategy=RolloutStrategy.CANARY, steps=(RolloutStep(0.5, "half"),))
    with pytest.raises(ValueError):
        RolloutPlan(strategy=RolloutStrategy.CANARY, steps=())


def test_strategy_factory() -> None:
    assert isinstance(strategy_for(RolloutStrategy.BLUE_GREEN), BlueGreenStrategy)
    assert isinstance(strategy_for(RolloutStrategy.CANARY), CanaryStrategy)
    assert isinstance(strategy_for(RolloutStrategy.RECREATE), RecreateStrategy)
