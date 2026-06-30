"""Unit tests for the age-rating / content-advisory tagger (pure aggregation)."""

from __future__ import annotations

from app.safety.advisory import AdvisoryAccumulator, rate_findings
from app.safety.contracts import AgeRating, Finding, SafetyCategory
from app.safety.taxonomy import Severity


def test_clean_book_rates_g() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.SAFE, 0.0)])
    assert advisory.rating is AgeRating.G
    assert advisory.descriptors == []


def test_mild_violence_rates_pg() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.VIOLENCE, 0.3)])  # LOW
    assert advisory.rating is AgeRating.PG
    assert any("violence" in d for d in advisory.descriptors)


def test_strong_gore_rates_r() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.GORE, 0.7)])  # HIGH
    assert advisory.rating is AgeRating.R


def test_strictest_band_wins_across_categories() -> None:
    advisory = rate_findings(
        [
            Finding.of(SafetyCategory.VIOLENCE, 0.3),  # PG
            Finding.of(SafetyCategory.SEXUAL, 0.5),  # R
        ]
    )
    assert advisory.rating is AgeRating.R


def test_sexual_minors_forces_nc17() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.SEXUAL_MINORS, 0.3)])
    assert advisory.rating is AgeRating.NC17


def test_category_severity_recorded() -> None:
    advisory = rate_findings(
        [
            Finding.of(SafetyCategory.VIOLENCE, 0.3),
            Finding.of(SafetyCategory.VIOLENCE, 0.9),  # worse — should win
        ]
    )
    assert advisory.category_severity[SafetyCategory.VIOLENCE] is Severity.CRITICAL


def test_accumulator_matches_flat_rating() -> None:
    findings = [
        Finding.of(SafetyCategory.VIOLENCE, 0.6),
        Finding.of(SafetyCategory.PROFANITY, 0.3),
    ]
    acc = AdvisoryAccumulator()
    acc.add([findings[0]])
    acc.add([findings[1]])
    assert acc.result().rating is rate_findings(findings).rating


def test_descriptors_have_severity_prefix() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.VIOLENCE, 0.9)])  # CRITICAL
    assert any("extreme" in d for d in advisory.descriptors)


def test_rationale_is_explainable() -> None:
    advisory = rate_findings([Finding.of(SafetyCategory.SEXUAL, 0.5)])
    assert advisory.rationale.startswith("rated R")
