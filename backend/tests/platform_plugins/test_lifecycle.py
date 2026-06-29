"""Lifecycle state-machine unit tests: install/enable/upgrade/rollback/quarantine."""

from __future__ import annotations

import pytest

from app.platform.plugins.errors import LifecycleError
from app.platform.plugins.lifecycle import (
    LifecycleAction,
    PluginState,
    install,
)
from app.platform.plugins.version import Version


def _v(s: str) -> Version:
    return Version.parse(s)


def test_install_starts_in_installed_state() -> None:
    inst = install("com.a.p", _v("1.0.0"))
    assert inst.state is PluginState.INSTALLED
    assert not inst.is_active
    assert inst.history == ()


def test_enable_activates_and_records_history() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    assert inst.state is PluginState.ENABLED
    assert inst.is_active
    assert [str(r.version) for r in inst.history] == ["1.0.0"]


def test_disable_then_enable() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable().disable()
    assert inst.state is PluginState.DISABLED
    inst = inst.enable()
    assert inst.state is PluginState.ENABLED


def test_cannot_disable_when_not_enabled() -> None:
    inst = install("com.a.p", _v("1.0.0"))
    assert not inst.can(LifecycleAction.DISABLE)
    with pytest.raises(LifecycleError):
        inst.disable()


def test_upgrade_then_commit() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    upgrading = inst.begin_upgrade(_v("1.1.0"))
    assert upgrading.state is PluginState.UPGRADING
    assert upgrading.version == _v("1.1.0")
    committed = upgrading.enable()
    assert committed.state is PluginState.ENABLED
    assert [str(r.version) for r in committed.history] == ["1.0.0", "1.1.0"]


def test_upgrade_to_same_version_rejected() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    with pytest.raises(LifecycleError, match="equals current"):
        inst.begin_upgrade(_v("1.0.0"))


def test_rollback_to_previous_version() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    inst = inst.begin_upgrade(_v("2.0.0")).enable()
    assert inst.version == _v("2.0.0")
    rolled = inst.rollback()
    assert rolled.version == _v("1.0.0")
    assert rolled.state is PluginState.ENABLED


def test_rollback_with_no_history_raises() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()  # only one version in history
    with pytest.raises(LifecycleError, match="no previous version"):
        inst.rollback()


def test_rollback_to_unknown_version_rejected() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    inst = inst.begin_upgrade(_v("2.0.0")).enable()
    with pytest.raises(LifecycleError, match="not in the version history"):
        inst.rollback(to=_v("9.9.9"))


def test_quarantine_after_failure_threshold() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    for _ in range(4):
        inst = inst.record_failure(quarantine_threshold=5)
        assert inst.state is PluginState.ENABLED
    inst = inst.record_failure(quarantine_threshold=5)
    assert inst.state is PluginState.QUARANTINED


def test_enable_resets_failure_count() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    inst = inst.record_failure().record_failure()
    assert inst.failure_count == 2
    inst = inst.disable().enable()
    assert inst.failure_count == 0


def test_quarantined_can_only_enable_or_uninstall() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable()
    for _ in range(5):
        inst = inst.record_failure(quarantine_threshold=5)
    assert inst.state is PluginState.QUARANTINED
    assert inst.can(LifecycleAction.ENABLE)
    assert inst.can(LifecycleAction.UNINSTALL)
    assert not inst.can(LifecycleAction.DISABLE)


def test_uninstall_is_terminal() -> None:
    inst = install("com.a.p", _v("1.0.0")).enable().disable().uninstall()
    assert inst.state is PluginState.UNINSTALLED
    for action in LifecycleAction:
        assert not inst.can(action)


def test_immutability_of_transitions() -> None:
    a = install("com.a.p", _v("1.0.0"))
    b = a.enable()
    assert a.state is PluginState.INSTALLED  # original unchanged
    assert b.state is PluginState.ENABLED
