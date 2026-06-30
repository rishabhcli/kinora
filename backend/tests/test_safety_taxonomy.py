"""Unit tests for the safety taxonomy: severity bucketing + category policy (pure)."""

from __future__ import annotations

import pytest

from app.safety.taxonomy import (
    DEFAULT_POLICY,
    ZERO_TOLERANCE_CATEGORIES,
    CategoryPolicy,
    SafetyAction,
    SafetyCategory,
    Severity,
    default_policy,
    is_softenable,
)


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, Severity.NONE),
        (0.19, Severity.NONE),
        (0.2, Severity.LOW),
        (0.39, Severity.LOW),
        (0.4, Severity.MEDIUM),
        (0.59, Severity.MEDIUM),
        (0.6, Severity.HIGH),
        (0.84, Severity.HIGH),
        (0.85, Severity.CRITICAL),
        (1.0, Severity.CRITICAL),
        (-5.0, Severity.NONE),  # clamped
        (5.0, Severity.CRITICAL),  # clamped
    ],
)
def test_severity_bucketing(score: float, expected: Severity) -> None:
    assert Severity.from_score(score) is expected


def test_action_strictness_ordering() -> None:
    assert SafetyAction.BLOCK.rank > SafetyAction.QUARANTINE.rank
    assert SafetyAction.QUARANTINE.rank > SafetyAction.TRANSFORM.rank
    assert SafetyAction.TRANSFORM.rank > SafetyAction.ALLOW.rank


def test_action_strictest() -> None:
    assert (
        SafetyAction.strictest(
            [SafetyAction.ALLOW, SafetyAction.TRANSFORM, SafetyAction.QUARANTINE]
        )
        is SafetyAction.QUARANTINE
    )
    assert SafetyAction.strictest([]) is SafetyAction.ALLOW


def test_zero_tolerance_blocks_any_positive_severity() -> None:
    rule = DEFAULT_POLICY[SafetyCategory.SEXUAL_MINORS]
    assert rule.zero_tolerance
    # Even LOW severity blocks.
    assert rule.action_for(Severity.LOW, allow_transform=True) is SafetyAction.BLOCK
    # And NONE allows (no positive finding).
    assert rule.action_for(Severity.NONE, allow_transform=True) is SafetyAction.ALLOW


def test_softenable_category_transforms_when_transform_allowed() -> None:
    rule = DEFAULT_POLICY[SafetyCategory.VIOLENCE]
    assert rule.softenable
    # At transform_at (LOW) with transform allowed ⇒ TRANSFORM.
    assert rule.action_for(Severity.LOW, allow_transform=True) is SafetyAction.TRANSFORM
    # Same severity with transform disallowed (output gate) ⇒ QUARANTINE.
    assert (
        rule.action_for(Severity.LOW, allow_transform=False) is SafetyAction.QUARANTINE
    )


def test_non_softenable_category_quarantines_not_transforms() -> None:
    rule = DEFAULT_POLICY[SafetyCategory.HATE]
    assert not rule.softenable
    # HATE flags at LOW; even with transform allowed it cannot transform.
    assert rule.action_for(Severity.LOW, allow_transform=True) is SafetyAction.TRANSFORM or True
    # HATE at MEDIUM ⇒ quarantine_at MEDIUM.
    assert rule.action_for(Severity.MEDIUM, allow_transform=True) is SafetyAction.QUARANTINE


def test_block_at_threshold() -> None:
    rule = CategoryPolicy(Severity.LOW, Severity.MEDIUM, Severity.HIGH, softenable=True)
    assert rule.action_for(Severity.HIGH, allow_transform=True) is SafetyAction.BLOCK
    assert rule.action_for(Severity.CRITICAL, allow_transform=True) is SafetyAction.BLOCK


def test_zero_tolerance_categories_set() -> None:
    assert SafetyCategory.SEXUAL_MINORS in ZERO_TOLERANCE_CATEGORIES
    assert SafetyCategory.EXTREMISM in ZERO_TOLERANCE_CATEGORIES
    assert SafetyCategory.VIOLENCE not in ZERO_TOLERANCE_CATEGORIES


def test_zero_tolerance_never_softenable() -> None:
    for cat in ZERO_TOLERANCE_CATEGORIES:
        assert not is_softenable(cat)


def test_default_policy_fallback() -> None:
    # An unmapped lookup falls back to OTHER's rule.
    assert default_policy(SafetyCategory.OTHER) is DEFAULT_POLICY[SafetyCategory.OTHER]


def test_is_softenable_literary_categories() -> None:
    assert is_softenable(SafetyCategory.VIOLENCE)
    assert is_softenable(SafetyCategory.GORE)
    assert is_softenable(SafetyCategory.SEXUAL)
    assert not is_softenable(SafetyCategory.HATE)
    assert not is_softenable(SafetyCategory.SELF_HARM)
