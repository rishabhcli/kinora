"""Unit tests for selection policies + capability filtering. Pure ranking over a
deterministic RouteContext; no network."""

from __future__ import annotations

import pytest

from app.providers.types import WanMode
from app.video.routing.capabilities import ProviderProfile, normalize_profiles
from app.video.routing.policy import (
    CapabilityFilteredPolicy,
    CheapestCapablePolicy,
    FastestPolicy,
    HighestQualityPolicy,
    PolicyKind,
    RouteContext,
    WeightedBlendPolicy,
    build_policy,
)


class StubHealth:
    """A scriptable HealthView: per-name success-rate + p95 latency."""

    def __init__(
        self,
        success: dict[str, float] | None = None,
        p95: dict[str, float] | None = None,
    ) -> None:
        self._success = success or {}
        self._p95 = p95 or {}

    def success_rate(self, name: str) -> float:
        return self._success.get(name, 1.0)

    def p50_latency_ms(self, name: str) -> float:
        return self._p95.get(name, 0.0)

    def p95_latency_ms(self, name: str) -> float:
        return self._p95.get(name, 0.0)


def _ctx(
    candidates: tuple[str, ...],
    profiles: dict[str, ProviderProfile],
    *,
    mode: WanMode = WanMode.TEXT_TO_VIDEO,
    budget_low: bool = False,
    health: StubHealth | None = None,
) -> RouteContext:
    return RouteContext(
        candidates=candidates,
        profiles=normalize_profiles(profiles),
        health=health or StubHealth(),
        mode=mode,
        budget_low=budget_low,
    )


_TURBO = ProviderProfile(cost_per_s=1.0, quality=0.4, est_latency_s=20.0)
_QUALITY = ProviderProfile(cost_per_s=4.0, quality=0.9, est_latency_s=80.0)


# --------------------------------------------------------------------------- #
# cheapest / fastest / highest-quality
# --------------------------------------------------------------------------- #


def test_cheapest_orders_by_cost() -> None:
    ctx = _ctx(("quality", "turbo"), {"turbo": _TURBO, "quality": _QUALITY})
    assert CheapestCapablePolicy().rank(ctx) == ["turbo", "quality"]


def test_highest_quality_orders_by_quality() -> None:
    ctx = _ctx(("turbo", "quality"), {"turbo": _TURBO, "quality": _QUALITY})
    assert HighestQualityPolicy().rank(ctx) == ["quality", "turbo"]


def test_fastest_uses_static_hint_before_observed() -> None:
    ctx = _ctx(("quality", "turbo"), {"turbo": _TURBO, "quality": _QUALITY})
    # no observed latency yet -> static hint: turbo (20s) faster than quality (80s)
    assert FastestPolicy().rank(ctx) == ["turbo", "quality"]


def test_fastest_prefers_observed_latency() -> None:
    # turbo's static hint is faster, but observed p95 makes quality the fast one.
    health = StubHealth(p95={"turbo": 90_000.0, "quality": 10_000.0})
    ctx = _ctx(("turbo", "quality"), {"turbo": _TURBO, "quality": _QUALITY}, health=health)
    assert FastestPolicy().rank(ctx) == ["quality", "turbo"]


def test_stable_tie_break_keeps_priority_order() -> None:
    same = ProviderProfile(cost_per_s=2.0, quality=0.5, est_latency_s=30.0)
    ctx = _ctx(("a", "b", "c"), {"a": same, "b": same, "c": same})
    assert CheapestCapablePolicy().rank(ctx) == ["a", "b", "c"]
    assert HighestQualityPolicy().rank(ctx) == ["a", "b", "c"]
    assert FastestPolicy().rank(ctx) == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# weighted blend
# --------------------------------------------------------------------------- #


def test_weighted_blend_quality_wins_when_advantage_outweighs_cost() -> None:
    # A quality backend that is only modestly pricier/slower beats a turbo on the
    # default blend (the quality term dominates a small cost/latency penalty).
    turbo = ProviderProfile(cost_per_s=1.0, quality=0.4, est_latency_s=25.0)
    near_quality = ProviderProfile(cost_per_s=1.5, quality=0.9, est_latency_s=30.0)
    ctx = _ctx(("turbo", "q"), {"turbo": turbo, "q": near_quality})
    assert WeightedBlendPolicy().rank(ctx)[0] == "q"


def test_weighted_blend_can_be_tuned_quality_dominant() -> None:
    # A quality-dominant blend picks the high-fidelity backend even when it is much
    # pricier/slower (cost/latency weights near zero).
    policy = WeightedBlendPolicy(
        cost_weight=0.0, latency_weight=0.0, quality_weight=0.9, success_weight=0.1
    )
    ctx = _ctx(("turbo", "quality"), {"turbo": _TURBO, "quality": _QUALITY})
    assert policy.rank(ctx)[0] == "quality"


def test_weighted_blend_leans_cheaper_under_budget_pressure() -> None:
    # A blend tuned to weight cost heavily flips to turbo when budget_low.
    policy = WeightedBlendPolicy(cost_weight=0.6, quality_weight=0.2, budget_low_cost_boost=3.0)
    healthy = _ctx(("turbo", "quality"), {"turbo": _TURBO, "quality": _QUALITY}, budget_low=False)
    low = _ctx(("turbo", "quality"), {"turbo": _TURBO, "quality": _QUALITY}, budget_low=True)
    assert policy.rank(low)[0] == "turbo"
    # And budget pressure should not make turbo *worse* than in the healthy case.
    assert policy.rank(healthy).index("turbo") >= policy.rank(low).index("turbo")


def test_weighted_blend_rewards_success_rate() -> None:
    # Two identical profiles; the healthier backend ranks first.
    same = ProviderProfile(cost_per_s=2.0, quality=0.5, est_latency_s=30.0)
    health = StubHealth(success={"a": 0.2, "b": 1.0})
    ctx = _ctx(("a", "b"), {"a": same, "b": same}, health=health)
    assert WeightedBlendPolicy().rank(ctx) == ["b", "a"]


# --------------------------------------------------------------------------- #
# capability filtering
# --------------------------------------------------------------------------- #


def test_capability_filter_drops_incapable_backends() -> None:
    t2v_only = ProviderProfile(modes=frozenset({WanMode.TEXT_TO_VIDEO}))
    full = ProviderProfile(modes=frozenset(WanMode))
    inner = CheapestCapablePolicy()
    policy = CapabilityFilteredPolicy(inner)
    ctx = _ctx(
        ("t2v", "full"),
        {"t2v": t2v_only, "full": full},
        mode=WanMode.IMAGE_TO_VIDEO,
    )
    assert policy.rank(ctx) == ["full"]  # t2v-only can't do i2v


def test_capability_filter_empty_when_none_capable() -> None:
    t2v_only = ProviderProfile(modes=frozenset({WanMode.TEXT_TO_VIDEO}))
    policy = CapabilityFilteredPolicy(CheapestCapablePolicy())
    ctx = _ctx(("t2v",), {"t2v": t2v_only}, mode=WanMode.REFERENCE_TO_VIDEO)
    assert policy.rank(ctx) == []


def test_capability_filter_name_wraps_inner() -> None:
    policy = CapabilityFilteredPolicy(CheapestCapablePolicy())
    assert "cheapest_capable" in policy.name


def test_unprofiled_backend_is_fully_capable() -> None:
    # No profile entry -> neutral -> supports every mode.
    policy = CapabilityFilteredPolicy(CheapestCapablePolicy())
    ctx = _ctx(("x", "y"), {}, mode=WanMode.FIRST_LAST_FRAME)
    assert policy.rank(ctx) == ["x", "y"]


# --------------------------------------------------------------------------- #
# build_policy
# --------------------------------------------------------------------------- #


def test_build_policy_by_kind() -> None:
    assert isinstance(build_policy(PolicyKind.CHEAPEST), CheapestCapablePolicy)
    assert isinstance(build_policy("fastest"), FastestPolicy)
    assert isinstance(build_policy("highest_quality"), HighestQualityPolicy)
    assert isinstance(build_policy("weighted"), WeightedBlendPolicy)
    with pytest.raises(ValueError):
        build_policy("nope")


def test_profile_validation() -> None:
    with pytest.raises(ValueError):
        ProviderProfile(cost_per_s=-1.0)
    with pytest.raises(ValueError):
        ProviderProfile(quality=2.0)
    with pytest.raises(ValueError):
        ProviderProfile(weight=0.0)
