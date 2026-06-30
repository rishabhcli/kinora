"""ServiceMeshSettings: defaults + env overrides (additive, package-local)."""

from __future__ import annotations

from app.servicemesh.compatibility import CompatibilityMode
from app.servicemesh.settings import ServiceMeshSettings


def test_defaults_are_production_safe() -> None:
    s = ServiceMeshSettings()
    assert s.default_compatibility is CompatibilityMode.BACKWARD
    assert s.enforce_gate is True
    assert s.stable_only is True
    assert s.validate_payloads is True
    assert s.max_conversion_hops == 16


def test_env_overrides(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SERVICEMESH_ENFORCE_GATE", "false")
    monkeypatch.setenv("SERVICEMESH_DEFAULT_COMPATIBILITY", "full")
    monkeypatch.setenv("SERVICEMESH_MAX_CONVERSION_HOPS", "4")
    s = ServiceMeshSettings()
    assert s.enforce_gate is False
    assert s.default_compatibility is CompatibilityMode.FULL
    assert s.max_conversion_hops == 4
