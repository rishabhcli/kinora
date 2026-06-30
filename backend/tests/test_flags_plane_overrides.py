"""Override layer, targeting rules, and sticky percentage rollouts (pure)."""

from __future__ import annotations

from app.flags.hashing import TOTAL_BASIS_POINTS
from app.flags.plane.context import FlagContext
from app.flags.plane.overrides import (
    OverrideLayer,
    PercentRollout,
    TargetingRule,
)


def test_rule_specificity_counts_constrained_dimensions() -> None:
    assert TargetingRule(id="a", value=1).specificity == 0
    assert TargetingRule(id="b", value=1, cohort="beta").specificity == 1
    assert TargetingRule(id="c", value=1, book="b1", user="u1").specificity == 2


def test_rule_matches_only_when_all_constrained_dims_match() -> None:
    rule = TargetingRule(id="r", value="x", cohort="beta", provider="minimax")
    assert rule.matches(FlagContext(cohort="beta", provider="minimax")) is True
    assert rule.matches(FlagContext(cohort="beta", provider="dashscope")) is False
    assert rule.matches(FlagContext(cohort="beta")) is False  # provider unset != minimax


def test_rule_unconstrained_dimension_matches_anything() -> None:
    rule = TargetingRule(id="r", value="x")  # no constraints -> matches all
    assert rule.matches(FlagContext()) is True
    assert rule.matches(FlagContext(book="b", user="u")) is True


def test_override_layer_is_immutable_and_versioned() -> None:
    layer = OverrideLayer()
    assert layer.version == 0
    l2 = layer.set_static("k", True)
    assert layer.version == 0  # original untouched
    assert l2.version == 1
    assert l2.overlay_for("k").static is not None
    assert l2.overlay_for("k").static.value is True


def test_add_and_remove_rule_round_trip() -> None:
    layer = OverrideLayer().add_rule("k", TargetingRule(id="r1", value=1, cohort="beta"))
    assert len(layer.overlay_for("k").rules) == 1
    # adding the same id replaces, not duplicates
    layer = layer.add_rule("k", TargetingRule(id="r1", value=2, cohort="beta"))
    assert len(layer.overlay_for("k").rules) == 1
    assert layer.overlay_for("k").rules[0].value == 2
    layer = layer.remove_rule("k", "r1")
    assert layer.overlay_for("k").rules == ()


def test_clear_flag_drops_all_overlays() -> None:
    layer = (
        OverrideLayer()
        .set_static("k", True)
        .add_rule("k", TargetingRule(id="r", value=False, cohort="beta"))
    )
    layer = layer.clear_flag("k")
    assert "k" not in layer.overlays


def test_layer_round_trips_through_dict() -> None:
    layer = (
        OverrideLayer()
        .set_static("a", True)
        .add_rule("b", TargetingRule(id="r", value="x", cohort="beta", priority=5))
        .set_rollout("c", PercentRollout(flag_key="c", percent=25.0, bucket_by="user"))
    )
    restored = OverrideLayer.from_dict(layer.to_dict())
    assert restored.overlay_for("a").static.value is True
    assert restored.overlay_for("b").rules[0].cohort == "beta"
    assert restored.overlay_for("b").rules[0].priority == 5
    assert restored.overlay_for("c").rollout.percent == 25.0


# --- sticky percentage rollout -------------------------------------------- #


def test_rollout_zero_and_hundred_percent() -> None:
    zero = PercentRollout(flag_key="f", percent=0.0)
    full = PercentRollout(flag_key="f", percent=100.0)
    ctx = FlagContext(user="u1")
    assert zero.admits(ctx) is False
    assert full.admits(ctx) is True


def test_rollout_excludes_context_without_bucketing_unit() -> None:
    # bucket_by=user but no user on the context -> excluded (fail safe).
    rollout = PercentRollout(flag_key="f", percent=100.0, bucket_by="user")
    assert rollout.admits(FlagContext(cohort="beta")) is False


def test_rollout_is_deterministic_and_sticky() -> None:
    # Determinism: same unit + percent -> same answer every call.
    r = PercentRollout(flag_key="f", percent=40.0, bucket_by="user")
    ctx = FlagContext(user="reader-123")
    assert r.admits(ctx) == r.admits(ctx)

    # Stickiness: anyone admitted at 10% is still admitted at 25% (monotone).
    admitted_at_10 = {
        f"u{i}"
        for i in range(2000)
        if PercentRollout(flag_key="f", percent=10.0).admits(FlagContext(user=f"u{i}"))
    }
    admitted_at_25 = {
        f"u{i}"
        for i in range(2000)
        if PercentRollout(flag_key="f", percent=25.0).admits(FlagContext(user=f"u{i}"))
    }
    assert admitted_at_10 <= admitted_at_25  # no one falls out as the ramp grows


def test_rollout_distribution_is_roughly_uniform() -> None:
    n = 5000
    admitted = sum(
        1
        for i in range(n)
        if PercentRollout(flag_key="f", percent=30.0).admits(FlagContext(user=f"user-{i}"))
    )
    fraction = admitted / n
    # 30% target; allow a generous band for a 5000-sample hash distribution.
    assert 0.27 < fraction < 0.33


def test_rollout_seed_reshuffles_membership() -> None:
    base = {
        f"u{i}"
        for i in range(3000)
        if PercentRollout(flag_key="f", percent=50.0, seed=0).admits(FlagContext(user=f"u{i}"))
    }
    reshuffled = {
        f"u{i}"
        for i in range(3000)
        if PercentRollout(flag_key="f", percent=50.0, seed=1).admits(FlagContext(user=f"u{i}"))
    }
    # Same target size, different membership after a reseed.
    assert base != reshuffled
    assert abs(len(base) - len(reshuffled)) < 0.05 * 3000


def test_total_basis_points_constant() -> None:
    assert TOTAL_BASIS_POINTS == 10_000
