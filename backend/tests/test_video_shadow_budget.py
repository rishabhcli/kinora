"""Eval budget: zero-by-default guard, hard cap, reserve/settle/release accounting."""

from __future__ import annotations

import pytest

from app.video.shadow.budget import EvalBudget, EvalBudgetExhausted


def test_default_budget_is_unfunded_and_refuses_everything() -> None:
    budget = EvalBudget()
    assert not budget.is_funded
    assert budget.remaining() == 0.0
    assert not budget.can_reserve(5.0)
    # Even a zero-cost reservation is refused on an unfunded pool (total guard).
    assert not budget.can_reserve(0.0)
    with pytest.raises(EvalBudgetExhausted):
        budget.reserve("shot", 5.0)


def test_explicit_zero_funding_still_refuses() -> None:
    budget = EvalBudget(cap_video_seconds=0.0)
    assert not budget.is_funded
    with pytest.raises(EvalBudgetExhausted):
        budget.reserve("shot", 1.0)


def test_negative_funding_clamped_to_zero() -> None:
    budget = EvalBudget(cap_video_seconds=-100.0)
    assert not budget.is_funded
    with pytest.raises(EvalBudgetExhausted):
        budget.reserve("shot", 1.0)


def test_funded_budget_reserves_within_cap() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    assert budget.is_funded
    res = budget.reserve("shot", 4.0)
    snap = budget.snapshot()
    assert snap.reserved_video_seconds == 4.0
    assert snap.remaining_video_seconds == 6.0
    budget.settle(res, 4.0)
    snap2 = budget.snapshot()
    assert snap2.reserved_video_seconds == 0.0
    assert snap2.committed_video_seconds == 4.0
    assert snap2.remaining_video_seconds == 6.0


def test_hard_cap_refuses_overdraw_atomically() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    budget.reserve("a", 6.0)
    # 6 reserved; 5 more would overdraw the 10s cap → refuse, nothing reserved.
    with pytest.raises(EvalBudgetExhausted):
        budget.reserve("b", 5.0)
    assert budget.snapshot().reserved_video_seconds == 6.0
    # A fitting reservation still succeeds.
    budget.reserve("c", 4.0)
    assert budget.remaining() == 0.0


def test_settle_reconciles_measured_not_estimated() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    res = budget.reserve("shot", 5.0)  # estimate
    budget.settle(res, 6.5)  # provider actually billed more
    assert budget.snapshot().committed_video_seconds == 6.5
    assert budget.snapshot().reserved_video_seconds == 0.0


def test_release_frees_reservation_without_spending() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    res = budget.reserve("shot", 5.0)
    budget.release(res)
    snap = budget.snapshot()
    assert snap.reserved_video_seconds == 0.0
    assert snap.committed_video_seconds == 0.0
    assert snap.remaining_video_seconds == 10.0


def test_settle_clamps_negative_measured_to_zero() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    res = budget.reserve("shot", 5.0)
    budget.settle(res, -3.0)
    assert budget.snapshot().committed_video_seconds == 0.0


def test_snapshot_is_immutable_view() -> None:
    budget = EvalBudget(cap_video_seconds=10.0)
    snap = budget.snapshot()
    budget.reserve("shot", 5.0)
    # The earlier snapshot is a frozen value, not a live view.
    assert snap.reserved_video_seconds == 0.0
    assert snap.is_funded
