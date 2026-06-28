"""Unit tests for prompt diffing + bump suggestion (no infra)."""

from __future__ import annotations

from app.llmops.diff import (
    diff_prompts,
    section_diff,
    suggest_bump,
    token_change_stats,
    unified_text_diff,
)


def test_identical_prompts() -> None:
    d = diff_prompts("same text", "same text")
    assert d.identical
    assert d.tokens.changed == 0
    assert d.tokens.jaccard == 1.0
    assert not d.sections.any_change
    assert d.summary() == "no change"
    assert unified_text_diff("x", "x") == ""


def test_token_change_stats() -> None:
    stats = token_change_stats("the quick brown fox", "the quick red fox")
    assert stats.added == 1  # "red"
    assert stats.removed == 1  # "brown"
    assert stats.common == 3
    assert 0.0 < stats.jaccard < 1.0


def test_section_diff_detects_added_removed_changed() -> None:
    old = "Intro.\nGUARDRAILS: never invent.\nReturn JSON: yes."
    new = "Intro.\nGUARDRAILS: never invent and never leak.\nMOTION: lots."
    sd = section_diff(old, new)
    assert "MOTION" in sd.added
    assert "Return JSON" in sd.removed
    assert "GUARDRAILS" in sd.changed


def test_suggest_bump_removed_section_is_major() -> None:
    old = "Body.\nGUARDRAILS: x.\nReturn JSON: y."
    new = "Body.\nGUARDRAILS: x."
    d = diff_prompts(old, new)
    assert suggest_bump(d) == "major"


def test_suggest_bump_added_section_is_minor() -> None:
    old = "Body text here."
    new = "Body text here.\nGUARDRAILS: never invent."
    d = diff_prompts(old, new)
    assert suggest_bump(d) == "minor"


def test_suggest_bump_contract_change_is_minor() -> None:
    old = "GUARDRAILS: be nice and gentle to the reader at all times."
    new = "GUARDRAILS: you must return JSON only and never include prose."
    d = diff_prompts(old, new)
    assert suggest_bump(d) == "minor"


def test_suggest_bump_small_wording_is_patch() -> None:
    # A 1-2 word tone tweak within a long, otherwise-identical section is a PATCH
    # (the jaccard stays high and few tokens move).
    old = (
        "NOTE: please be concise and direct when describing the scene, keeping the "
        "tone warm and the language vivid so the reader can picture every moment of "
        "the unfolding action across the whole page without any wasted words."
    )
    new = old.replace("concise", "terse")
    d = diff_prompts(old, new)
    assert d.tokens.changed == 2
    assert suggest_bump(d) == "patch"


def test_summary_mentions_sections() -> None:
    old = "A.\nGUARDRAILS: x."
    new = "A.\nGUARDRAILS: x.\nEXTRA: y."
    d = diff_prompts(old, new)
    summary = d.summary()
    assert "EXTRA" in summary
    assert "token" in summary
