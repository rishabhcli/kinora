"""Unit tests for the versioned prompt registry (no infra)."""

from __future__ import annotations

import pytest

from app.llmops.errors import DuplicateVersionError, PromptNotFoundError, RollbackError
from app.llmops.registry import ChangeKind, PromptRegistry, VersionStatus


def test_seeded_from_agents_loads_crew_prompts() -> None:
    reg = PromptRegistry.seeded_from_agents()
    # The eight crew prompt keys are present.
    for key in ("adapter", "cinematographer", "critic", "showrunner", "continuity"):
        assert reg.has(key)
        active = reg.get_active(key)
        assert active.status is VersionStatus.ACTIVE
    # cinematographer@v3 -> 3.0.0 baseline.
    assert reg.get_active("cinematographer").version == "3.0.0"


def test_register_auto_bumps_and_activates() -> None:
    reg = PromptRegistry()
    reg.register("adapter", "SYSTEM v1 base prompt.", author="me")
    assert reg.get_active("adapter").version == "1.0.0"
    # A guardrail-section change auto-suggests a minor bump.
    rec = reg.register(
        "adapter", "SYSTEM v1 base prompt.\nGUARDRAILS: return JSON only.", author="me"
    )
    assert rec.version == "1.1.0"
    assert reg.get_active("adapter").version == "1.1.0"
    # The prior version was archived (exactly one ACTIVE per key).
    assert reg.get("adapter", "1.0.0").status is VersionStatus.ARCHIVED


def test_register_explicit_bump() -> None:
    reg = PromptRegistry()
    reg.register("k", "base")
    rec = reg.register("k", "base + breaking change", bump="major")
    assert rec.version == "2.0.0"


def test_duplicate_body_rejected() -> None:
    reg = PromptRegistry()
    reg.register("k", "identical body")
    with pytest.raises(DuplicateVersionError):
        reg.register("k", "identical body")


def test_rollback_to_previous() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    reg.register("k", "v2 changed", bump="minor")
    reg.register("k", "v3 changed again", bump="minor")
    assert reg.get_active("k").version == "1.2.0"
    rolled = reg.rollback("k")
    assert rolled.version == "1.1.0"
    assert reg.get_active("k").version == "1.1.0"


def test_rollback_explicit_target() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    reg.register("k", "v2", bump="minor")
    reg.register("k", "v3", bump="minor")
    rolled = reg.rollback("k", to="1.0.0")
    assert rolled.version == "1.0.0"


def test_rollback_forward_rejected() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    reg.register("k", "v2", bump="minor")
    reg.rollback("k", to="1.0.0")
    with pytest.raises(RollbackError):
        reg.rollback("k", to="1.1.0")  # that's forward, not back


def test_rollback_no_lower_version_raises() -> None:
    reg = PromptRegistry()
    reg.register("k", "only version")
    with pytest.raises(RollbackError):
        reg.rollback("k")


def test_promote_draft() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    draft = reg.register("k", "v2 draft", bump="minor", activate=False)
    assert draft.status is VersionStatus.DRAFT
    assert reg.get_active("k").version == "1.0.0"
    promoted = reg.promote("k", draft.version)
    assert promoted.status is VersionStatus.ACTIVE
    assert reg.get_active("k").version == draft.version


def test_unknown_key_raises() -> None:
    reg = PromptRegistry()
    with pytest.raises(PromptNotFoundError):
        reg.get_active("nope")
    with pytest.raises(PromptNotFoundError):
        reg.versions("nope")


def test_changelog_records_events() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    reg.register("k", "v2", bump="minor")
    reg.rollback("k")
    kinds = [e.kind for e in reg.changelog("k")]
    assert ChangeKind.REGISTER in kinds
    assert ChangeKind.ROLLBACK in kinds
    # Filtering by key works.
    assert all(e.key == "k" for e in reg.changelog("k"))


def test_diff_between_versions() -> None:
    reg = PromptRegistry()
    reg.register("k", "base body text")
    reg.register("k", "base body text\nGUARDRAILS: no leaks", bump="minor")
    d = reg.diff("k", old="1.0.0", new="1.1.0")
    assert not d.identical
    assert "GUARDRAILS" in d.sections.added


def test_latest_vs_active_differ_after_rollback() -> None:
    reg = PromptRegistry()
    reg.register("k", "v1")
    reg.register("k", "v2", bump="minor")
    reg.rollback("k")
    assert reg.get_active("k").version == "1.0.0"
    assert reg.latest("k").version == "1.1.0"  # latest is still the highest semver
