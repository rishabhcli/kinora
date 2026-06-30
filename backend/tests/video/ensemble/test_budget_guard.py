"""Budget-guard tests: the fail-closed fan-out gate cascade, per-shot cost-cap
enforcement at launch, and reservation accounting. No network, no real budget."""

from __future__ import annotations

import pytest

from app.video.ensemble.budget_guard import (
    CostCapExceeded,
    FanOutRefusal,
    MultiRenderBudgetGuard,
)
from app.video.ensemble.models import CostUnit, EnsembleConfig, Objective, ProviderChoice

from ._fakes import FakeBudget, spec


def _cfg(**kw: object) -> EnsembleConfig:
    base: dict[str, object] = {
        "enabled": True,
        "enabled_tiers": frozenset({"hero"}),
        "max_candidates": 3,
    }
    base.update(kw)
    return EnsembleConfig(**base)


def _guard(cfg: EnsembleConfig, *, live: bool = True) -> MultiRenderBudgetGuard:
    return MultiRenderBudgetGuard(FakeBudget(live=live), cfg)


# --------------------------------------------------------------------------- #
# The fail-closed gate cascade
# --------------------------------------------------------------------------- #


def test_disabled_by_default_refuses_fanout() -> None:
    # The bare default config (enabled=False, max_candidates=1) must never fan out.
    guard = _guard(EnsembleConfig())
    decision = guard.decide(spec(tier="hero"))
    assert decision.allowed is False
    assert decision.refusal is FanOutRefusal.DISABLED


def test_tier_not_enabled_refuses() -> None:
    guard = _guard(_cfg(enabled_tiers=frozenset({"hero"})))
    decision = guard.decide(spec(tier="standard"))
    assert decision.allowed is False
    assert decision.refusal is FanOutRefusal.TIER_NOT_ENABLED


def test_single_candidate_refuses() -> None:
    guard = _guard(_cfg(max_candidates=1))
    decision = guard.decide(spec(tier="hero"))
    assert decision.allowed is False
    assert decision.refusal is FanOutRefusal.SINGLE_CANDIDATE


def test_live_gate_off_refuses() -> None:
    guard = _guard(_cfg(), live=False)
    decision = guard.decide(spec(tier="hero"))
    assert decision.allowed is False
    assert decision.refusal is FanOutRefusal.LIVE_GATE_OFF


def test_all_conditions_met_allows_fanout() -> None:
    guard = _guard(_cfg())
    decision = guard.decide(spec(tier="hero"))
    assert decision.allowed is True
    assert decision.refusal is FanOutRefusal.ALLOWED


def test_gate_cascade_priority_disabled_first() -> None:
    # Disabled beats every other failing condition (checked first).
    cfg = EnsembleConfig(enabled=False, enabled_tiers=frozenset(), max_candidates=1)
    guard = MultiRenderBudgetGuard(FakeBudget(live=False), cfg)
    assert guard.decide(spec(tier="standard")).refusal is FanOutRefusal.DISABLED


# --------------------------------------------------------------------------- #
# Reservation + per-shot cost cap
# --------------------------------------------------------------------------- #


async def test_reserve_sizes_seconds_by_duration_and_cost_per_s() -> None:
    budget = FakeBudget()
    guard = MultiRenderBudgetGuard(budget, _cfg())
    choice = ProviderChoice(name="p", cost_per_s=2.0)
    reservation = await guard.try_reserve(spec(duration_s=5.0), choice)
    assert reservation.video_seconds == pytest.approx(10.0)  # 5s * 2.0
    assert budget.ledger.reserved == pytest.approx(10.0)
    assert guard.reserved_video_seconds == pytest.approx(10.0)


async def test_cost_cap_aborts_launch_that_would_breach() -> None:
    budget = FakeBudget()
    cfg = _cfg(per_shot_cost_cap=12.0, cost_unit=CostUnit.VIDEO_SECONDS)
    guard = MultiRenderBudgetGuard(budget, cfg)
    s = spec(duration_s=5.0)
    # First candidate: 5*1.0 = 5s reserved (under the 12s cap).
    await guard.try_reserve(s, ProviderChoice(name="a", cost_per_s=1.0))
    # Second candidate would add 8s → 13s > 12s cap → abort, nothing extra reserved.
    with pytest.raises(CostCapExceeded):
        await guard.try_reserve(s, ProviderChoice(name="b", cost_per_s=1.6))
    assert budget.ledger.reserved == pytest.approx(5.0)  # only the first stuck
    assert guard.reserved_video_seconds == pytest.approx(5.0)


async def test_cost_cap_usd_unit() -> None:
    budget = FakeBudget()
    cfg = _cfg(per_shot_cost_cap=0.50, cost_unit=CostUnit.USD)
    guard = MultiRenderBudgetGuard(budget, cfg)
    s = spec(duration_s=5.0)
    # 5s * $0.08/s = $0.40 (under $0.50).
    await guard.try_reserve(s, ProviderChoice(name="a", cost_per_s=1.0, usd_per_s=0.08))
    # 5s * $0.05/s = $0.25 → $0.65 > $0.50 cap.
    with pytest.raises(CostCapExceeded):
        await guard.try_reserve(s, ProviderChoice(name="b", cost_per_s=1.0, usd_per_s=0.05))
    assert guard.reserved_usd == pytest.approx(0.40)


async def test_would_exceed_cap_is_pure_query() -> None:
    guard = MultiRenderBudgetGuard(FakeBudget(), _cfg(per_shot_cost_cap=4.0))
    s = spec(duration_s=5.0)
    choice = ProviderChoice(name="p", cost_per_s=1.0)  # 5s > 4s cap
    assert guard.would_exceed_cap(s, choice) is True
    # No cap → never exceeds.
    open_guard = MultiRenderBudgetGuard(FakeBudget(), _cfg(per_shot_cost_cap=0.0))
    assert open_guard.would_exceed_cap(s, choice) is False


async def test_commit_and_release_settle_the_ledger() -> None:
    budget = FakeBudget()
    guard = MultiRenderBudgetGuard(budget, _cfg())
    r1 = await guard.try_reserve(spec(duration_s=5.0), ProviderChoice(name="win", cost_per_s=1.0))
    r2 = await guard.try_reserve(spec(duration_s=5.0), ProviderChoice(name="lose", cost_per_s=1.0))
    await guard.commit_winner(r1)
    await guard.release(r2)
    assert budget.commits == [r1.id]
    assert budget.releases == [r2.id]
    assert budget.net_committed == pytest.approx(5.0)
    assert budget.outstanding == {}


async def test_underlying_global_ceiling_propagates() -> None:
    # A ceiling on the ledger raises even when the per-shot cap is open.
    budget = FakeBudget(ceiling=3.0)
    guard = MultiRenderBudgetGuard(budget, _cfg(per_shot_cost_cap=0.0))
    with pytest.raises(RuntimeError, match="ceiling"):
        await guard.try_reserve(spec(duration_s=5.0), ProviderChoice(name="p", cost_per_s=1.0))


def test_objective_independent_of_gate() -> None:
    # The gate cascade doesn't depend on the objective.
    for obj in Objective:
        guard = _guard(_cfg(objective=obj))
        assert guard.decide(spec(tier="hero")).allowed is True
