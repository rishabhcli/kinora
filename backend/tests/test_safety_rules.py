"""Unit tests for the deterministic safety rule engine (pure: findings → action)."""

from __future__ import annotations

from app.safety.contracts import Finding, SafetyCategory
from app.safety.rules import (
    PolicyTable,
    evaluate,
    softenable_categories,
    unsoftenable_blocking_categories,
)
from app.safety.taxonomy import CategoryPolicy, SafetyAction, Severity


def test_clean_findings_allow() -> None:
    decision = evaluate([Finding.of(SafetyCategory.SAFE, 0.0)])
    assert decision.action is SafetyAction.ALLOW
    assert decision.driving == []
    assert decision.severity is Severity.NONE


def test_zero_tolerance_blocks_even_low_score() -> None:
    decision = evaluate([Finding.of(SafetyCategory.SEXUAL_MINORS, 0.25)])
    assert decision.action is SafetyAction.BLOCK
    assert SafetyCategory.SEXUAL_MINORS in decision.categories


def test_softenable_violence_transforms() -> None:
    decision = evaluate([Finding.of(SafetyCategory.VIOLENCE, 0.3)])
    assert decision.action is SafetyAction.TRANSFORM
    assert SafetyCategory.VIOLENCE in softenable_categories(decision)


def test_strictest_action_wins_across_findings() -> None:
    decision = evaluate(
        [
            Finding.of(SafetyCategory.VIOLENCE, 0.3),  # TRANSFORM
            Finding.of(SafetyCategory.GORE, 0.9),  # BLOCK (block_at HIGH; 0.9=CRITICAL)
        ]
    )
    assert decision.action is SafetyAction.BLOCK
    # Driving findings are those at/above the winning action.
    cats = decision.categories
    assert SafetyCategory.GORE in cats


def test_allow_transform_false_escalates_to_quarantine() -> None:
    # The output gate path: a would-be TRANSFORM becomes QUARANTINE.
    transform = evaluate([Finding.of(SafetyCategory.VIOLENCE, 0.3)], allow_transform=True)
    quarantine = evaluate([Finding.of(SafetyCategory.VIOLENCE, 0.3)], allow_transform=False)
    assert transform.action is SafetyAction.TRANSFORM
    assert quarantine.action is SafetyAction.QUARANTINE


def test_per_category_worst_severity_collapses() -> None:
    decision = evaluate(
        [
            Finding.of(SafetyCategory.VIOLENCE, 0.3),
            Finding.of(SafetyCategory.VIOLENCE, 0.9),
        ]
    )
    # Only one VIOLENCE entry resolved, at the worst severity.
    assert decision.per_category[SafetyCategory.VIOLENCE] is SafetyAction.BLOCK


def test_unsoftenable_blocking_categories() -> None:
    decision = evaluate(
        [
            Finding.of(SafetyCategory.HATE, 0.9),  # block, non-softenable
            Finding.of(SafetyCategory.VIOLENCE, 0.3),  # transform, softenable
        ]
    )
    unsoftenable = unsoftenable_blocking_categories(decision)
    assert SafetyCategory.HATE in unsoftenable
    assert SafetyCategory.VIOLENCE not in unsoftenable


def test_policy_overrides_cannot_relax_zero_tolerance() -> None:
    base = PolicyTable.builtin()
    # Attempt to make CSAM permissive — must be rejected by the floor re-assertion.
    permissive = CategoryPolicy(
        Severity.CRITICAL, Severity.CRITICAL, Severity.CRITICAL, zero_tolerance=False
    )
    table = base.with_overrides(
        {SafetyCategory.SEXUAL_MINORS: permissive}, version="evil"
    )
    decision = evaluate([Finding.of(SafetyCategory.SEXUAL_MINORS, 0.25)], policy=table)
    assert decision.action is SafetyAction.BLOCK


def test_policy_overrides_apply_to_non_floor_categories() -> None:
    base = PolicyTable.builtin()
    # Make violence stricter (block at LOW).
    strict = CategoryPolicy(Severity.LOW, Severity.LOW, Severity.LOW, softenable=False)
    table = base.with_overrides({SafetyCategory.VIOLENCE: strict}, version="strict")
    decision = evaluate([Finding.of(SafetyCategory.VIOLENCE, 0.3)], policy=table)
    assert decision.action is SafetyAction.BLOCK
    assert table.version == "strict"
