"""Unit tests for the moderation taxonomy: severity bucketing + dispositions (pure)."""

from __future__ import annotations

import pytest

from app.moderation.taxonomy import (
    DEFAULT_DISPOSITIONS,
    ZERO_TOLERANCE_CATEGORIES,
    CategoryRule,
    Disposition,
    ModerationCategory,
    Severity,
    default_rule,
)


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, Severity.NONE),
        (0.1, Severity.NONE),
        (0.2, Severity.LOW),
        (0.39, Severity.LOW),
        (0.4, Severity.MEDIUM),
        (0.59, Severity.MEDIUM),
        (0.6, Severity.HIGH),
        (0.84, Severity.HIGH),
        (0.85, Severity.CRITICAL),
        (1.0, Severity.CRITICAL),
        (5.0, Severity.CRITICAL),  # clamps
        (-1.0, Severity.NONE),  # clamps
    ],
)
def test_severity_from_score_buckets(score: float, expected: Severity) -> None:
    assert Severity.from_score(score) is expected


def test_severity_is_ordered() -> None:
    assert Severity.NONE < Severity.LOW < Severity.MEDIUM < Severity.HIGH < Severity.CRITICAL


def test_disposition_strictest() -> None:
    assert Disposition.strictest([]) is Disposition.ALLOW
    assert (
        Disposition.strictest([Disposition.ALLOW, Disposition.FLAG]) is Disposition.FLAG
    )
    assert (
        Disposition.strictest([Disposition.FLAG, Disposition.BLOCK, Disposition.ALLOW])
        is Disposition.BLOCK
    )


def test_category_rule_thresholds() -> None:
    rule = CategoryRule(flag_at=Severity.MEDIUM, block_at=Severity.HIGH)
    assert rule.disposition_for(Severity.NONE) is Disposition.ALLOW
    assert rule.disposition_for(Severity.LOW) is Disposition.ALLOW
    assert rule.disposition_for(Severity.MEDIUM) is Disposition.FLAG
    assert rule.disposition_for(Severity.HIGH) is Disposition.BLOCK
    assert rule.disposition_for(Severity.CRITICAL) is Disposition.BLOCK


def test_zero_tolerance_blocks_any_positive() -> None:
    rule = CategoryRule(flag_at=Severity.LOW, block_at=Severity.LOW, zero_tolerance=True)
    assert rule.disposition_for(Severity.NONE) is Disposition.ALLOW
    assert rule.disposition_for(Severity.LOW) is Disposition.BLOCK
    assert rule.disposition_for(Severity.CRITICAL) is Disposition.BLOCK


def test_csam_and_extremism_are_zero_tolerance() -> None:
    assert ModerationCategory.SEXUAL_MINORS in ZERO_TOLERANCE_CATEGORIES
    assert ModerationCategory.EXTREMISM in ZERO_TOLERANCE_CATEGORIES
    assert default_rule(ModerationCategory.SEXUAL_MINORS).zero_tolerance is True


def test_default_dispositions_cover_every_category() -> None:
    for cat in ModerationCategory:
        assert cat in DEFAULT_DISPOSITIONS, f"missing default rule for {cat}"


def test_default_rule_falls_back_to_other() -> None:
    # default_rule never raises for a known category and uses OTHER as the fallback rule.
    assert default_rule(ModerationCategory.OTHER) is DEFAULT_DISPOSITIONS[ModerationCategory.OTHER]
