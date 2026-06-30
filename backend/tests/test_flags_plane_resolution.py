"""Layered resolution precedence: base -> static -> rule -> rollout (pure)."""

from __future__ import annotations

from app.flags.plane.context import FlagContext
from app.flags.plane.overrides import (
    OverrideLayer,
    PercentRollout,
    TargetingRule,
)
from app.flags.plane.resolution import LayeredResolver, ResolutionSource
from app.flags.plane.spec import FlagSpec, FlagType

RESOLVER = LayeredResolver()


def _bool_spec(default: bool = False, *, kill_switch: bool = False) -> FlagSpec:
    return FlagSpec(key="f", type=FlagType.BOOL, default=default, kill_switch=kill_switch)


def _string_spec(default: str = "dashscope") -> FlagSpec:
    return FlagSpec(
        key="video.backend",
        type=FlagType.STRING,
        default=default,
        choices=("dashscope", "minimax"),
    )


def test_base_value_when_no_overlay() -> None:
    res = RESOLVER.resolve(_bool_spec(True), OverrideLayer(), FlagContext())
    assert res.value is True
    assert res.source is ResolutionSource.BASE


def test_static_override_beats_base() -> None:
    layer = OverrideLayer().set_static("f", True)
    res = RESOLVER.resolve(_bool_spec(False), layer, FlagContext())
    assert res.value is True
    assert res.source is ResolutionSource.STATIC_OVERRIDE


def test_matching_rule_beats_static() -> None:
    spec = _string_spec("dashscope")
    layer = (
        OverrideLayer()
        .set_static("video.backend", "dashscope")
        .add_rule("video.backend", TargetingRule(id="beta", value="minimax", cohort="beta"))
    )
    beta = RESOLVER.resolve(spec, layer, FlagContext(cohort="beta"))
    assert beta.value == "minimax"
    assert beta.source is ResolutionSource.TARGETING_RULE
    assert beta.rule_id == "beta"
    # Non-matching context falls back to the static override.
    other = RESOLVER.resolve(spec, layer, FlagContext(cohort="ga"))
    assert other.value == "dashscope"
    assert other.source is ResolutionSource.STATIC_OVERRIDE


def test_most_specific_rule_wins() -> None:
    spec = _string_spec("dashscope")
    layer = (
        OverrideLayer()
        # broad: any beta cohort
        .add_rule("video.backend", TargetingRule(id="cohort", value="minimax", cohort="beta"))
        # specific: beta cohort AND a particular book
        .add_rule(
            "video.backend",
            TargetingRule(id="book", value="dashscope", cohort="beta", book="b1"),
        )
    )
    # The 2-dimension rule beats the 1-dimension rule for the matching context.
    res = RESOLVER.resolve(spec, layer, FlagContext(cohort="beta", book="b1"))
    assert res.rule_id == "book"
    assert res.value == "dashscope"


def test_rule_tie_broken_by_priority() -> None:
    spec = _string_spec("dashscope")
    lo = TargetingRule(id="lo", value="minimax", cohort="beta", priority=1)
    hi = TargetingRule(id="hi", value="dashscope", cohort="beta", priority=9)
    layer = OverrideLayer().add_rule("video.backend", lo).add_rule("video.backend", hi)
    res = RESOLVER.resolve(spec, layer, FlagContext(cohort="beta"))
    assert res.rule_id == "hi"


def test_flag_level_rollout_serves_on_to_admitted_only() -> None:
    spec = _bool_spec(False)
    layer = OverrideLayer().set_rollout("f", PercentRollout(flag_key="f", percent=100.0))
    res = RESOLVER.resolve(spec, layer, FlagContext(user="u1"))
    assert res.value is True
    assert res.source is ResolutionSource.ROLLOUT
    assert res.in_rollout is True

    excluded_layer = OverrideLayer().set_rollout("f", PercentRollout(flag_key="f", percent=0.0))
    excl = RESOLVER.resolve(spec, excluded_layer, FlagContext(user="u1"))
    assert excl.value is False  # excluded -> keeps the base/off
    assert excl.in_rollout is False


def test_kill_switch_clamped_on_read_even_if_layer_says_on() -> None:
    spec = _bool_spec(False, kill_switch=True)
    # A hand-crafted layer that (illegally) tries to force the kill-switch on.
    layer = OverrideLayer().set_static("f", True)
    res = RESOLVER.resolve(spec, layer, FlagContext())
    assert res.value is False  # clamped back down
    assert res.source is ResolutionSource.KILL_SWITCH_CLAMP
    assert res.raw_value is True  # the pre-clamp value is preserved for diagnostics


def test_resolution_is_total_and_never_raises() -> None:
    # A malformed rule value for the type degrades to base rather than raising.
    spec = FlagSpec(key="f", type=FlagType.INT, default=3)
    layer = OverrideLayer().set_static("f", "not-an-int")
    res = RESOLVER.resolve(spec, layer, FlagContext())
    assert res.value == 3
    assert res.source is ResolutionSource.BASE
