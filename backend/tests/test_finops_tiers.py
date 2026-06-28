"""Tiered budget caps + alert levels (kinora.md §11.1). Pure — no infra."""

from __future__ import annotations

import math

import pytest

from app.core.config import Settings
from app.finops.tiers import (
    AlertLevel,
    BudgetScopeKind,
    BudgetTierPolicy,
    TieredCap,
    TierThresholds,
)


def _cap(cap_s: float, **kw: float) -> TieredCap:
    return TieredCap(BudgetScopeKind.SESSION, cap_s, TierThresholds(**kw))


def test_thresholds_must_be_non_decreasing() -> None:
    with pytest.raises(ValueError):
        TierThresholds(info=0.8, warning=0.5, soft=0.9)
    with pytest.raises(ValueError):
        TierThresholds(info=-0.1)
    # Valid is accepted.
    TierThresholds(info=0.1, warning=0.2, soft=0.3)


def test_level_for_maps_fraction_to_level() -> None:
    t = TierThresholds(info=0.5, warning=0.75, soft=0.9)
    assert t.level_for(0.0) is AlertLevel.OK
    assert t.level_for(0.49) is AlertLevel.OK
    assert t.level_for(0.5) is AlertLevel.INFO
    assert t.level_for(0.8) is AlertLevel.WARNING
    assert t.level_for(0.95) is AlertLevel.SOFT_CAP
    assert t.level_for(1.0) is AlertLevel.HARD_CAP
    assert t.level_for(1.5) is AlertLevel.HARD_CAP
    assert t.level_for(float("nan")) is AlertLevel.HARD_CAP


def test_cap_fraction_and_headroom() -> None:
    cap = _cap(100.0)
    status = cap.evaluate(75.0)
    assert status.fraction == pytest.approx(0.75)
    assert status.headroom_s == pytest.approx(25.0)
    assert status.level is AlertLevel.WARNING
    assert status.soft_cap_s == pytest.approx(90.0)
    assert not status.exhausted
    assert not status.at_or_over_soft


def test_cap_at_or_over_soft_and_exhausted() -> None:
    cap = _cap(100.0)
    assert cap.evaluate(90.0).at_or_over_soft
    assert cap.evaluate(100.0).exhausted
    assert cap.evaluate(150.0).exhausted
    # Headroom never goes negative.
    assert cap.headroom_s(150.0) == 0.0


def test_zero_cap_is_fully_consumed_not_a_zero_division() -> None:
    cap = _cap(0.0)
    status = cap.evaluate(0.0)
    assert status.fraction == 1.0
    assert status.level is AlertLevel.HARD_CAP


def test_infinite_cap_never_binds() -> None:
    cap = TieredCap(BudgetScopeKind.TENANT, math.inf)
    status = cap.evaluate(10_000.0)
    assert status.fraction == 0.0
    assert status.level is AlertLevel.OK
    assert math.isinf(status.headroom_s)
    assert not cap.would_exceed_hard(10_000.0, 10_000.0)
    assert not cap.would_exceed_soft(10_000.0, 10_000.0)


def test_would_exceed_hard_and_soft() -> None:
    cap = _cap(100.0)  # soft = 90
    assert not cap.would_exceed_hard(50.0, 50.0)
    assert cap.would_exceed_hard(50.0, 51.0)
    assert not cap.would_exceed_soft(40.0, 50.0)
    assert cap.would_exceed_soft(50.0, 41.0)


def test_policy_from_settings_builds_all_scopes() -> None:
    settings = Settings(dashscope_api_key="test")
    policy = BudgetTierPolicy.from_settings(settings)
    assert policy.global_cap.cap_s == settings.budget_ceiling_video_s
    assert policy.session_cap.cap_s == settings.budget_per_session_s
    assert policy.scene_cap.cap_s == settings.budget_per_scene_s
    # No tenant cap configured (<= 0) -> infinite (single-tenant local/demo).
    assert math.isinf(policy.tenant_cap.cap_s)


def test_policy_tenant_cap_honoured_when_set() -> None:
    settings = Settings(dashscope_api_key="test", finops_tenant_ceiling_video_s=500.0)
    policy = BudgetTierPolicy.from_settings(settings)
    assert policy.tenant_cap.cap_s == 500.0


def test_policy_evaluate_all_and_worst_and_binding() -> None:
    settings = Settings(
        dashscope_api_key="test",
        budget_ceiling_video_s=1000.0,
        budget_per_session_s=100.0,
        budget_per_scene_s=50.0,
    )
    policy = BudgetTierPolicy.from_settings(settings)
    used = {
        BudgetScopeKind.GLOBAL: 100.0,  # 10%
        BudgetScopeKind.SESSION: 95.0,  # 95% -> soft
        BudgetScopeKind.SCENE: 10.0,  # 20%
    }
    statuses = policy.evaluate_all(used)
    assert [s.scope for s in statuses] == [
        BudgetScopeKind.GLOBAL,
        BudgetScopeKind.SESSION,
        BudgetScopeKind.SCENE,
    ]
    assert BudgetTierPolicy.worst_level(statuses) is AlertLevel.SOFT_CAP
    binding = BudgetTierPolicy.binding_scope(statuses)
    assert binding is not None
    # Session has the least headroom (5s vs scene 40s vs global 900s).
    assert binding.scope is BudgetScopeKind.SESSION
    assert binding.headroom_s == pytest.approx(5.0)


def test_worst_level_empty_is_ok() -> None:
    assert BudgetTierPolicy.worst_level([]) is AlertLevel.OK


def test_alert_level_label_roundtrip() -> None:
    for level in AlertLevel:
        assert AlertLevel.from_label(level.label) is level
    assert AlertLevel.from_label("garbage") is AlertLevel.OK
