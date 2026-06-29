"""Capability-model unit tests: hierarchy, catalog, deny-by-default grants."""

from __future__ import annotations

import pytest

from app.platform.plugins.capabilities import (
    CAPABILITY_CATALOG,
    Capability,
    GrantSet,
    RiskTier,
    expand_catalog,
    is_known_capability,
    risk_of,
)
from app.platform.plugins.errors import CapabilityDeniedError, PluginValidationError


def test_capability_parses_dotted_scope() -> None:
    cap = Capability("canon.read")
    assert cap.root == "canon"
    assert cap.segments == ("canon", "read")


@pytest.mark.parametrize("bad", ["", "Canon", "1canon", "canon.", ".read", "a..b", "canon read"])
def test_invalid_scope_rejected(bad: str) -> None:
    with pytest.raises(PluginValidationError):
        Capability(bad)


def test_implies_is_prefix_hierarchy() -> None:
    assert Capability("canon").implies(Capability("canon.read"))
    assert Capability("canon").implies(Capability("canon"))
    assert not Capability("canon.read").implies(Capability("canon.write"))
    assert not Capability("canon.read").implies(Capability("canon"))
    # 'can' must not imply 'canon.read' — prefix is dotted-segment-aware.
    assert not Capability("canon").implies(Capability("canonical.read"))


def test_grantset_normalizes_subsumed_grants() -> None:
    gs = GrantSet.of("canon", "canon.read", "canon.write")
    # canon subsumes the children -> only 'canon' is kept.
    assert gs.to_sorted() == ("canon",)


def test_grantset_permits_and_require() -> None:
    gs = GrantSet.of("canon.read", "log.write")
    assert gs.permits("canon.read")
    assert not gs.permits("canon.write")
    gs.require("canon.read")  # no raise
    with pytest.raises(CapabilityDeniedError):
        gs.require("canon.write")


def test_empty_grantset_denies_all() -> None:
    gs = GrantSet.of()
    assert not gs.permits("log.write")


def test_unknown_capability_rejected_at_construction() -> None:
    with pytest.raises(PluginValidationError):
        GrantSet.of("filesystem.write")
    with pytest.raises(PluginValidationError):
        GrantSet.of("totally.made.up")


def test_parent_grant_is_known() -> None:
    # 'canon' is a known parent because 'canon.read' / 'canon.write' exist.
    assert is_known_capability("canon")
    assert is_known_capability("canon.read")
    assert not is_known_capability("filesystem")


def test_risk_of_parent_inherits_max_child_risk() -> None:
    # canon.* includes canon.write (HIGH) -> the parent grant is HIGH risk.
    assert risk_of("canon") is RiskTier.HIGH
    assert risk_of("canon.read") is RiskTier.LOW
    assert risk_of("net.fetch") is RiskTier.HIGH


def test_grantset_max_risk() -> None:
    assert GrantSet.of("log.write", "book.read").max_risk is RiskTier.LOW
    assert GrantSet.of("log.write", "net.fetch").max_risk is RiskTier.HIGH


def test_is_subset_of() -> None:
    narrow = GrantSet.of("canon.read")
    broad = GrantSet.of("canon")
    assert narrow.is_subset_of(broad)
    assert not broad.is_subset_of(narrow)


def test_expand_catalog_covers_children() -> None:
    specs = expand_catalog("canon")
    scopes = {s.scope for s in specs}
    assert {"canon.read", "canon.query", "canon.write"} <= scopes


def test_catalog_is_nonempty_and_well_formed() -> None:
    assert CAPABILITY_CATALOG
    for scope, spec in CAPABILITY_CATALOG.items():
        assert spec.scope == scope
        assert isinstance(spec.risk, RiskTier)
        assert Capability(scope)  # every catalog scope parses
