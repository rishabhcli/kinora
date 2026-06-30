"""Registry lifecycle + health: activate, disable/enable, quarantine, routable."""

from __future__ import annotations

import pytest

from app.video.plugins.conformance import CaseResult, ConformanceReport
from app.video.plugins.contracts import RenderMode
from app.video.plugins.errors import PluginNotFoundError, RegistryStateError
from app.video.plugins.loader import LoadedPlugin, PluginLoader
from app.video.plugins.registry import PluginRegistry, PluginState
from app.video.plugins.sandbox import CapabilityGrant, HostServices

from .conftest import GoodPlugin, make_manifest, resolver_for


def _loaded(plugin_id: str = "com.acme.good") -> LoadedPlugin:
    manifest = make_manifest(plugin_id=plugin_id)
    loader = PluginLoader(resolver=resolver_for(GoodPlugin))
    return loader.load(
        manifest, config={}, grant=CapabilityGrant(frozenset()), services=HostServices()
    )


def _report(ref: str, *, passed: bool) -> ConformanceReport:
    results = (CaseResult("c", passed=passed, required=True),)
    return ConformanceReport(plugin_ref=ref, results=results)


def test_activate_then_routable() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    entry = reg.register_active(loaded, _report(loaded.ref, passed=True))
    assert entry.state is PluginState.ACTIVE
    assert reg.routable() == (entry,)
    assert reg.supporting(RenderMode.TEXT_TO_VIDEO) == (entry,)
    assert reg.supporting(RenderMode.IMAGE_TO_VIDEO) == ()


def test_cannot_activate_failing_report() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    with pytest.raises(RegistryStateError):
        reg.register_active(loaded, _report(loaded.ref, passed=False))


def test_quarantine_is_not_routable() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    report = ConformanceReport(
        plugin_ref=loaded.ref,
        results=(CaseResult("generate_honours_request", passed=False, required=True),),
    )
    entry = reg.register_quarantined(loaded, report)
    assert entry.state is PluginState.QUARANTINED
    assert entry.quarantine_failures == ("generate_honours_request",)
    assert reg.routable() == ()
    assert reg.quarantined() == (entry,)


def test_disable_then_enable_roundtrip() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    reg.register_active(loaded, _report(loaded.ref, passed=True))
    reg.disable(loaded.manifest.id)
    assert reg.get(loaded.manifest.id).state is PluginState.DISABLED
    assert reg.routable() == ()
    reg.enable(loaded.manifest.id)
    assert reg.get(loaded.manifest.id).state is PluginState.ACTIVE
    assert len(reg.routable()) == 1


def test_enable_quarantined_requires_passing_report() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    reg.register_quarantined(loaded, _report(loaded.ref, passed=False))
    # No report ⇒ refuse.
    with pytest.raises(RegistryStateError):
        reg.enable(loaded.manifest.id)
    # Failing report ⇒ refuse.
    with pytest.raises(RegistryStateError):
        reg.enable(loaded.manifest.id, _report(loaded.ref, passed=False))
    # Passing report ⇒ clears the quarantine.
    entry = reg.enable(loaded.manifest.id, _report(loaded.ref, passed=True))
    assert entry.state is PluginState.ACTIVE
    assert entry.quarantine_failures == ()


def test_cannot_disable_quarantined() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    reg.register_quarantined(loaded, _report(loaded.ref, passed=False))
    with pytest.raises(RegistryStateError):
        reg.disable(loaded.manifest.id)


def test_health_demotes_after_consecutive_failures() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    reg.register_active(loaded, _report(loaded.ref, passed=True))
    pid = loaded.manifest.id
    reg.record_health(pid, ok=False, error="probe timeout")
    assert reg.get(pid).health.is_healthy  # one failure tolerated
    assert len(reg.routable()) == 1
    reg.record_health(pid, ok=False, error="probe timeout")
    assert not reg.get(pid).health.is_healthy  # two in a row ⇒ unhealthy
    assert reg.routable() == ()  # ACTIVE but unhealthy is not routed
    reg.record_health(pid, ok=True)  # recovery resets the streak
    assert reg.get(pid).health.is_healthy
    assert len(reg.routable()) == 1


def test_remove_and_missing() -> None:
    reg = PluginRegistry()
    loaded = _loaded()
    reg.register_active(loaded, _report(loaded.ref, passed=True))
    reg.remove(loaded.manifest.id)
    assert not reg.contains(loaded.manifest.id)
    with pytest.raises(PluginNotFoundError):
        reg.get(loaded.manifest.id)
    with pytest.raises(PluginNotFoundError):
        reg.remove("never.registered")
