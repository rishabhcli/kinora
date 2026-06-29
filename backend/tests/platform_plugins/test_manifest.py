"""Manifest parsing/validation unit tests."""

from __future__ import annotations

import pytest

from app.platform.plugins.capabilities import RiskTier
from app.platform.plugins.errors import PluginValidationError
from app.platform.plugins.hooks import ExtensionPoint
from app.platform.plugins.manifest import HOST_API_VERSION, PluginManifest


def _base(**overrides: object) -> dict:
    data = {
        "id": "com.acme.tone",
        "version": "1.2.0",
        "name": "Tone Filter",
        "capabilities": ["book.read", "log.write"],
        "hooks": [{"id": "h1", "point": "ingest.filter", "entrypoint": "run"}],
    }
    data.update(overrides)
    return data


def test_parse_minimal_valid() -> None:
    m = PluginManifest.parse(_base())
    assert m.id == "com.acme.tone"
    assert m.ref == "com.acme.tone@1.2.0"
    assert m.hooks[0].point is ExtensionPoint.INGEST_FILTER
    assert m.supports_host(HOST_API_VERSION)
    assert not m.requires_review
    assert m.max_risk is RiskTier.LOW


def test_roundtrip_to_dict_and_back() -> None:
    m = PluginManifest.parse(_base(dependencies=[{"plugin_id": "com.acme.base", "range": "^1.0"}]))
    again = PluginManifest.parse(m.to_dict())
    assert again.to_dict() == m.to_dict()


@pytest.mark.parametrize("bad_id", ["acme", "Acme.Tone", "com.acme.", "com..tone", "1com.acme"])
def test_invalid_id_rejected(bad_id: str) -> None:
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(id=bad_id))


def test_unknown_capability_rejected() -> None:
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(capabilities=["filesystem.write"]))


def test_duplicate_hook_id_rejected() -> None:
    hooks = [
        {"id": "dup", "point": "ingest.filter", "entrypoint": "a"},
        {"id": "dup", "point": "render.postprocess", "entrypoint": "b"},
    ]
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(hooks=hooks))


def test_self_dependency_rejected() -> None:
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(dependencies=[{"plugin_id": "com.acme.tone", "range": "*"}]))


def test_webhook_hook_requires_net_fetch() -> None:
    hooks = [{"id": "wh", "point": "webhook.action", "entrypoint": "notify"}]
    # Without net.fetch the coherence check fails.
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(capabilities=["log.write"], hooks=hooks))
    # With net.fetch it parses.
    m = PluginManifest.parse(_base(capabilities=["net.fetch"], hooks=hooks))
    assert m.requires_review  # net.fetch is HIGH risk


def test_high_risk_capability_requires_review() -> None:
    m = PluginManifest.parse(_base(capabilities=["canon.write"]))
    assert m.requires_review
    assert m.max_risk is RiskTier.HIGH


def test_invalid_entrypoint_rejected() -> None:
    hooks = [{"id": "h", "point": "ingest.filter", "entrypoint": "not an identifier"}]
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(hooks=hooks))


def test_import_allowlist_validated() -> None:
    m = PluginManifest.parse(_base(import_allowlist=["calendar", "zoneinfo"]))
    assert "calendar" in m.import_allowlist
    with pytest.raises(PluginValidationError):
        PluginManifest.parse(_base(import_allowlist=["not a module!"]))


def test_hooks_for_orders_by_priority() -> None:
    hooks = [
        {"id": "late", "point": "ingest.filter", "entrypoint": "a", "priority": 200},
        {"id": "early", "point": "ingest.filter", "entrypoint": "b", "priority": 50},
    ]
    m = PluginManifest.parse(_base(hooks=hooks))
    ordered = [h.id for h in m.hooks_for(ExtensionPoint.INGEST_FILTER)]
    assert ordered == ["early", "late"]


def test_api_version_gate() -> None:
    m = PluginManifest.parse(_base(api_version=">=2.0.0"))
    assert not m.supports_host(HOST_API_VERSION)
