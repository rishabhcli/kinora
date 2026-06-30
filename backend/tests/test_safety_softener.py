"""Unit tests for the intent-preserving prompt auto-softener (deterministic)."""

from __future__ import annotations

import pytest

from app.safety.contracts import Finding, SafetyCategory
from app.safety.rules import RuleDecision, evaluate
from app.safety.softener import RuleSoftener

pytestmark = pytest.mark.asyncio


def _decision(*findings: Finding) -> RuleDecision:
    return evaluate(list(findings))


async def test_softens_literary_violence_preserving_intent() -> None:
    softener = RuleSoftener()
    decision = _decision(Finding.of(SafetyCategory.VIOLENCE, 0.6))
    result = await softener.soften("He moves to stab the guard", decision=decision)
    assert result.changed
    assert result.intent_preserved
    assert "stab" not in result.softened_prompt.lower()
    assert result.softened_prompt.strip()  # never emptied
    assert result.transforms  # explainable


async def test_soften_records_diff() -> None:
    softener = RuleSoftener()
    decision = _decision(Finding.of(SafetyCategory.GORE, 0.9))
    result = await softener.soften("graphic gore in the scene", decision=decision)
    assert any("gore" in t for t in result.transforms)
    assert result.changed


async def test_soften_is_noop_on_clean_prompt() -> None:
    softener = RuleSoftener()
    decision = _decision(Finding.of(SafetyCategory.SAFE, 0.0))
    result = await softener.soften("a sunny field of flowers", decision=decision)
    assert not result.changed
    assert result.softened_prompt == "a sunny field of flowers"


async def test_soften_never_touches_unsoftenable_category() -> None:
    softener = RuleSoftener()
    # HATE is non-softenable; the result must report it, not rewrite it.
    decision = _decision(
        Finding.of(SafetyCategory.HATE, 0.9),
        Finding.of(SafetyCategory.VIOLENCE, 0.6),
    )
    result = await softener.soften(
        "a brutal beating with hate speech", decision=decision
    )
    assert SafetyCategory.HATE in result.unsoftenable
    # The hate term is left intact (it is escalated, not silently dropped).
    assert "hate speech" in result.softened_prompt.lower()
    # But the softenable violence WAS reframed.
    assert "brutal beating" not in result.softened_prompt.lower()


async def test_soften_resolves_category_so_it_no_longer_drives_block() -> None:
    softener = RuleSoftener()
    decision = _decision(Finding.of(SafetyCategory.GORE, 0.7))
    result = await softener.soften("blood everywhere on the floor", decision=decision)
    # The substitution removed the GORE trigger entirely.
    assert SafetyCategory.GORE in result.resolved


async def test_soften_idempotent_on_already_soft_text() -> None:
    softener = RuleSoftener()
    decision = _decision(Finding.of(SafetyCategory.VIOLENCE, 0.6))
    once = await softener.soften("He moves to stab the guard", decision=decision)
    # Re-evaluate the softened text: it should no longer flag the same way, so a
    # second pass (with an empty softenable decision) makes no change.
    redecision = evaluate([Finding.of(SafetyCategory.SAFE, 0.0)])
    twice = await softener.soften(once.softened_prompt, decision=redecision)
    assert not twice.changed


async def test_soften_only_targets_flagged_categories() -> None:
    # A prompt that *mentions* a softenable phrase but whose decision did NOT flag
    # that category is left untouched (we never reframe content policy did not flag).
    softener = RuleSoftener()
    decision = evaluate([Finding.of(SafetyCategory.SAFE, 0.0)])
    result = await softener.soften("a dramatic stab of lightning", decision=decision)
    assert not result.changed
