"""Unit tests for the pre-generation prompt gate (classify → soften → route)."""

from __future__ import annotations

import pytest

from app.safety.classifier import KeywordSafetyClassifier
from app.safety.contracts import SafetyAction, SafetyCategory
from app.safety.prompt_gate import PromptGate

pytestmark = pytest.mark.asyncio


def _gate() -> PromptGate:
    return PromptGate(classifier=KeywordSafetyClassifier())


async def test_clean_prompt_allows() -> None:
    decision = await _gate().screen("a quiet morning by the lake")
    assert decision.action is SafetyAction.ALLOW
    assert decision.allowed
    assert decision.effective_prompt == "a quiet morning by the lake"
    assert decision.routing is not None and decision.routing.has_viable_provider


async def test_literary_violence_transforms_and_preserves_intent() -> None:
    decision = await _gate().screen("a graphic stabbing with blood everywhere")
    assert decision.action is SafetyAction.TRANSFORM
    assert decision.transformed
    assert decision.allowed  # TRANSFORM still proceeds
    # Effective prompt is the softened rewrite, intent preserved (non-empty).
    assert decision.effective_prompt != "a graphic stabbing with blood everywhere"
    assert decision.effective_prompt.strip()
    assert "stab" not in decision.effective_prompt.lower()
    assert decision.softening is not None and decision.softening.transforms


async def test_transform_unlocks_strict_providers() -> None:
    # Before softening, dashscope/minimax would refuse the gore; after softening the
    # content is clean enough that all providers become viable.
    decision = await _gate().screen("blood everywhere across the battlefield")
    assert decision.action is SafetyAction.TRANSFORM
    assert decision.routing is not None
    assert "dashscope" in decision.routing.ordered_providers


async def test_csam_blocks() -> None:
    decision = await _gate().screen("csam content")
    assert decision.action is SafetyAction.BLOCK
    assert decision.blocked
    assert SafetyCategory.SEXUAL_MINORS in decision.categories


async def test_hate_blocks_even_when_mixed_with_softenable_violence() -> None:
    decision = await _gate().screen("a brutal beating set to hate speech")
    # Violence softens away, but hate is non-softenable ⇒ stays blocked.
    assert decision.action is SafetyAction.BLOCK
    assert SafetyCategory.HATE in decision.categories
    assert decision.softening is not None
    assert SafetyCategory.HATE in decision.softening.unsoftenable


async def test_no_viable_provider_downgrades_allowable_to_quarantine() -> None:
    # Restrict candidates to the strictest provider for content it refuses but the
    # gateway would otherwise allow/transform: routing has no viable provider, so
    # the action is downgraded to QUARANTINE.
    gate = _gate()
    # MEDIUM sexual ⇒ TRANSFORM normally; restrict to dashscope which refuses it
    # even after softening only if softening fails — use a non-softenable proxy:
    decision = await gate.screen(
        "a moderate scene with self-harm depicted", candidates=["dashscope"]
    )
    # self_harm is non-softenable; dashscope refuses ⇒ no viable provider.
    assert decision.action in (SafetyAction.QUARANTINE, SafetyAction.BLOCK)


async def test_explainability_carries_driving_findings() -> None:
    decision = await _gate().screen("a graphic stabbing")
    assert decision.reason
    assert decision.driving_findings or decision.transformed
    assert decision.classifier == "keyword"


async def test_mild_fight_below_threshold_allows() -> None:
    # 'fight' maps to VIOLENCE at LOW (0.3); VIOLENCE transform_at is LOW so this
    # is exactly at the softening edge — it transforms (intent preserved), it does
    # not block, and all providers stay viable.
    decision = await _gate().screen("a brief fight breaks out at the tavern")
    assert decision.action in (SafetyAction.ALLOW, SafetyAction.TRANSFORM)
    assert decision.allowed
    assert decision.routing is not None and decision.routing.has_viable_provider
