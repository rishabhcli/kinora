"""Tests for the budget-aware degradation advisor (§11.1)."""

from __future__ import annotations

import pytest

from app.providers.resilience.degradation import (
    BudgetWindow,
    DegradationAdvisor,
    DegradationLevel,
)
from app.providers.resilience.metering import MeteringSink
from app.providers.types import Usage


def test_no_degradation_with_plenty_of_budget() -> None:
    adv = DegradationAdvisor(BudgetWindow(cap_s=1650.0, floor_s=200.0, soft_margin_s=200.0))
    a = adv.advise(spent_video_seconds=100.0)
    assert a.level is DegradationLevel.NONE
    assert not a.should_degrade
    assert not a.budget_low
    assert a.remaining_s == 1550.0
    assert a.fraction_spent == pytest.approx(100.0 / 1650.0)


def test_soft_degradation_within_margin() -> None:
    adv = DegradationAdvisor(BudgetWindow(cap_s=1000.0, floor_s=100.0, soft_margin_s=150.0))
    # remaining = 1000 - 800 = 200, which is in (floor=100, floor+margin=250] -> SOFT.
    a = adv.advise(spent_video_seconds=800.0)
    assert a.level is DegradationLevel.SOFT
    assert a.should_degrade
    assert not a.budget_low  # SOFT is not the UI 'budget_low' signal


def test_hard_degradation_at_floor() -> None:
    adv = DegradationAdvisor(BudgetWindow(cap_s=1000.0, floor_s=100.0, soft_margin_s=150.0))
    # remaining = 1000 - 950 = 50 <= floor=100 -> HARD.
    a = adv.advise(spent_video_seconds=950.0)
    assert a.level is DegradationLevel.HARD
    assert a.should_degrade
    assert a.budget_low


def test_overspend_clamps_remaining_to_zero() -> None:
    adv = DegradationAdvisor(BudgetWindow(cap_s=500.0, floor_s=0.0))
    a = adv.advise(spent_video_seconds=600.0)
    assert a.remaining_s == 0.0
    assert a.level is DegradationLevel.HARD
    assert a.fraction_spent == 1.0


def test_advise_from_meter_reads_video_seconds() -> None:
    meter = MeteringSink()
    meter(Usage(model="wan2.1-i2v-turbo", operation="video", video_seconds=300.0))
    adv = DegradationAdvisor(BudgetWindow(cap_s=400.0, floor_s=150.0))
    a = adv.advise_from_meter(meter)
    # remaining = 400 - 300 = 100 <= floor 150 -> HARD.
    assert a.spent_s == 300.0
    assert a.level is DegradationLevel.HARD


def test_budget_window_validates() -> None:
    with pytest.raises(ValueError):
        BudgetWindow(cap_s=0.0)
    with pytest.raises(ValueError):
        BudgetWindow(cap_s=100.0, floor_s=-1.0)
    with pytest.raises(ValueError):
        BudgetWindow(cap_s=100.0, soft_margin_s=-1.0)
