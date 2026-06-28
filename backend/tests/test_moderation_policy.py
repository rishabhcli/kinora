"""Unit tests for the deterministic policy engine (pure: labels → verdict)."""

from __future__ import annotations

from app.moderation.contracts import ClassificationResult, ContentLabel, Surface
from app.moderation.policy import evaluate, merge_verdicts
from app.moderation.taxonomy import Disposition, ModerationCategory, Severity
from app.moderation.tenant_policy import builtin_policies


def _result(*labels: ContentLabel, surface: Surface = Surface.CLIP) -> ClassificationResult:
    return ClassificationResult(
        surface=surface,
        labels=list(labels) or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
        classifier="test",
    )


def test_clean_content_allows() -> None:
    verdict = evaluate(_result(ContentLabel.of(ModerationCategory.SAFE, 0.0)))
    assert verdict.decision is Disposition.ALLOW
    assert verdict.driving_labels == []
    assert verdict.severity is Severity.NONE


def test_csam_blocks_even_at_low_score() -> None:
    # Zero-tolerance: even a low-confidence positive label blocks.
    verdict = evaluate(_result(ContentLabel.of(ModerationCategory.SEXUAL_MINORS, 0.25)))
    assert verdict.decision is Disposition.BLOCK
    assert ModerationCategory.SEXUAL_MINORS in verdict.categories


def test_strictest_disposition_wins() -> None:
    verdict = evaluate(
        _result(
            ContentLabel.of(ModerationCategory.PROFANITY, 0.3),  # allow
            ContentLabel.of(ModerationCategory.VIOLENCE, 0.5),  # flag (default)
            ContentLabel.of(ModerationCategory.GORE, 0.9),  # block (default)
        )
    )
    assert verdict.decision is Disposition.BLOCK
    # Driving labels are only those at the strictest tier (the GORE block).
    assert all(lab.category is ModerationCategory.GORE for lab in verdict.driving_labels)


def test_flag_surfaces_medium_sexual() -> None:
    verdict = evaluate(_result(ContentLabel.of(ModerationCategory.SEXUAL, 0.5)))
    assert verdict.decision is Disposition.FLAG
    assert verdict.severity is Severity.MEDIUM


def test_children_policy_is_stricter_than_default() -> None:
    children = builtin_policies()["children"]
    # A mild sexual hint flags under default but blocks under the children policy.
    result = _result(ContentLabel.of(ModerationCategory.SEXUAL, 0.45))
    assert evaluate(result).decision is Disposition.FLAG
    assert evaluate(result, policy=children).decision is Disposition.BLOCK


def test_mature_policy_allows_depicted_sexual_but_not_minors() -> None:
    mature = builtin_policies()["mature"]
    sexual = _result(ContentLabel.of(ModerationCategory.SEXUAL, 0.7))
    assert evaluate(sexual, policy=mature).decision is Disposition.ALLOW
    # The zero-tolerance floor still blocks minors regardless of the override.
    minors = _result(ContentLabel.of(ModerationCategory.SEXUAL_MINORS, 0.5))
    assert evaluate(minors, policy=mature).decision is Disposition.BLOCK


def test_strictness_multiplier_escalates_tier() -> None:
    children = builtin_policies()["children"]  # strictness 1.4
    # 0.45 raw -> MEDIUM; *1.4 = 0.63 -> HIGH. The verdict severity reflects the rescale.
    verdict = evaluate(
        _result(ContentLabel.of(ModerationCategory.HATE, 0.45)), policy=children
    )
    assert verdict.severity >= Severity.HIGH


def test_effective_severity_restamped_on_driving_label() -> None:
    children = builtin_policies()["children"]
    verdict = evaluate(
        _result(ContentLabel.of(ModerationCategory.HATE, 0.45)), policy=children
    )
    assert verdict.driving_labels[0].severity is verdict.severity


def test_degraded_passthrough() -> None:
    result = ClassificationResult(
        surface=Surface.CLIP,
        labels=[ContentLabel.of(ModerationCategory.SAFE, 0.0)],
        classifier="test",
        degraded=True,
    )
    verdict = evaluate(result)
    assert verdict.degraded is True
    assert verdict.decision is Disposition.ALLOW  # the engine never upgrades degraded


def test_merge_verdicts_takes_strictest() -> None:
    frames = evaluate(_result(ContentLabel.of(ModerationCategory.VIOLENCE, 0.5)))  # flag
    narration = evaluate(
        _result(ContentLabel.of(ModerationCategory.HATE, 0.9), surface=Surface.NARRATION)
    )  # block
    merged = merge_verdicts([frames, narration])
    assert merged.decision is Disposition.BLOCK
    assert ModerationCategory.HATE in merged.categories


def test_merge_single_is_identity() -> None:
    v = evaluate(_result(ContentLabel.of(ModerationCategory.SEXUAL, 0.5)))
    assert merge_verdicts([v]) is v


def test_reason_is_human_readable() -> None:
    verdict = evaluate(_result(ContentLabel.of(ModerationCategory.GORE, 0.9)))
    assert "blocked" in verdict.reason
    assert "gore" in verdict.reason
