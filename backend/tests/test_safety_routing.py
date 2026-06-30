"""Unit tests for per-provider policy profiles + routing avoidance (pure)."""

from __future__ import annotations

from app.safety.contracts import Finding, SafetyCategory
from app.safety.profiles import ProfileRegistry, ProviderPolicyProfile
from app.safety.routing import plan_routing
from app.safety.taxonomy import Severity


def test_clean_prompt_all_providers_viable() -> None:
    plan = plan_routing([Finding.of(SafetyCategory.SAFE, 0.0)])
    assert plan.has_viable_provider
    # Every builtin provider is viable for clean content.
    assert set(plan.ordered_providers) == set(ProfileRegistry.builtin().providers())


def test_router_avoids_provider_that_refuses() -> None:
    # MEDIUM sexual: dashscope refuses at MEDIUM, minimax refuses at MEDIUM too,
    # but degrade/selfhost (permissive) tolerate it.
    plan = plan_routing([Finding.of(SafetyCategory.SEXUAL, 0.5)])
    assert "dashscope" not in plan.ordered_providers
    assert "degrade" in plan.ordered_providers
    assert SafetyCategory.SEXUAL in plan.avoided_categories


def test_zero_tolerance_no_viable_provider() -> None:
    plan = plan_routing([Finding.of(SafetyCategory.SEXUAL_MINORS, 0.3)])
    assert not plan.has_viable_provider
    assert plan.best_provider is None


def test_profile_floor_clamps_zero_tolerance_even_for_permissive_provider() -> None:
    # A maximally permissive profile still refuses CSAM (clamped by the floor).
    permissive = ProviderPolicyProfile(
        provider="anything",
        default_refuses_at=Severity.CRITICAL,
        permissiveness=1.0,
    )
    assert permissive.refuses(SafetyCategory.SEXUAL_MINORS, Severity.LOW)
    assert not permissive.refuses(SafetyCategory.VIOLENCE, Severity.LOW)


def test_ranking_explains_every_provider() -> None:
    plan = plan_routing([Finding.of(SafetyCategory.SEXUAL, 0.5)])
    providers_in_rankings = {r.provider for r in plan.rankings}
    assert providers_in_rankings == set(ProfileRegistry.builtin().providers())
    # The refusing providers carry their refusing categories.
    refusing = [r for r in plan.rankings if not r.viable]
    assert refusing
    assert all(r.refusing_categories for r in refusing)


def test_ordered_best_first_prefers_more_permissive() -> None:
    # Mild violence (LOW) is tolerated by all; the most permissive (degrade) ranks
    # first by tie-break.
    plan = plan_routing([Finding.of(SafetyCategory.VIOLENCE, 0.3)])
    assert plan.ordered_providers[0] == "degrade"


def test_candidates_restrict_the_plan() -> None:
    plan = plan_routing(
        [Finding.of(SafetyCategory.SAFE, 0.0)], candidates=["dashscope", "minimax"]
    )
    assert set(plan.ordered_providers) == {"dashscope", "minimax"}


def test_unknown_provider_treated_as_strict() -> None:
    reg = ProfileRegistry.builtin()
    profile = reg.get("mystery-provider")
    # Unknown ⇒ strict default (refuses at MEDIUM) and not viable for MEDIUM content.
    plan = plan_routing(
        [Finding.of(SafetyCategory.SEXUAL, 0.5)],
        registry=reg,
        candidates=["mystery-provider"],
    )
    assert not plan.has_viable_provider
    assert profile.permissiveness == 0.0


def test_register_replaces_profile() -> None:
    reg = ProfileRegistry.builtin()
    reg.register(
        ProviderPolicyProfile(
            provider="dashscope",
            default_refuses_at=Severity.CRITICAL,
            permissiveness=1.0,
        )
    )
    # Now dashscope tolerates MEDIUM sexual content.
    plan = plan_routing(
        [Finding.of(SafetyCategory.SEXUAL, 0.5)],
        registry=reg,
        candidates=["dashscope"],
    )
    assert "dashscope" in plan.ordered_providers
