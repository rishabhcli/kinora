"""The plugin lifecycle state machine — install / enable / upgrade / rollback.

A plugin installation moves through a small, explicit set of states. Transitions
are *pure functions* over the current installation and the requested action, so
the legal-move policy is exhaustively unit-testable without a database. The
persistence layer (:mod:`app.platform.plugins.store`) simply applies the
returned next state.

States:

* ``INSTALLED`` — present but not active; no hooks registered.
* ``ENABLED`` — active; hooks are live and dispatched.
* ``DISABLED`` — explicitly turned off (distinct from never-enabled).
* ``UPGRADING`` — a transient state during a version change (the store sets the
  new version, re-resolves dependencies, then transitions to ``ENABLED`` or
  rolls back).
* ``QUARANTINED`` — auto-disabled by the host after repeated runtime failures
  (a circuit breaker); requires an explicit ``enable`` to leave.
* ``UNINSTALLED`` — terminal; the row is retained for audit/rollback history.

The installation tracks a **version ledger**: every enabled version is appended
to ``history`` so ``rollback`` can return to the immediately previous good
version deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from app.platform.plugins.errors import LifecycleError
from app.platform.plugins.version import Version


class PluginState(StrEnum):
    """The lifecycle states a plugin installation can occupy."""

    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UPGRADING = "upgrading"
    QUARANTINED = "quarantined"
    UNINSTALLED = "uninstalled"

    @property
    def is_active(self) -> bool:
        """True when the plugin's hooks should be live."""
        return self is PluginState.ENABLED


class LifecycleAction(StrEnum):
    """The operations callers request against an installation."""

    INSTALL = "install"
    ENABLE = "enable"
    DISABLE = "disable"
    UPGRADE = "upgrade"
    ROLLBACK = "rollback"
    QUARANTINE = "quarantine"
    UNINSTALL = "uninstall"


#: Legal (from_state -> {actions}) transition table. ``INSTALL`` is special: it
#: applies to a *non-existent* installation, handled by :func:`install`.
_TRANSITIONS: dict[PluginState, frozenset[LifecycleAction]] = {
    PluginState.INSTALLED: frozenset(
        {LifecycleAction.ENABLE, LifecycleAction.UPGRADE, LifecycleAction.UNINSTALL}
    ),
    PluginState.ENABLED: frozenset(
        {
            LifecycleAction.DISABLE,
            LifecycleAction.UPGRADE,
            LifecycleAction.ROLLBACK,
            LifecycleAction.QUARANTINE,
            LifecycleAction.UNINSTALL,
        }
    ),
    PluginState.DISABLED: frozenset(
        {LifecycleAction.ENABLE, LifecycleAction.UPGRADE, LifecycleAction.UNINSTALL}
    ),
    PluginState.QUARANTINED: frozenset({LifecycleAction.ENABLE, LifecycleAction.UNINSTALL}),
    PluginState.UPGRADING: frozenset(
        {LifecycleAction.ENABLE, LifecycleAction.ROLLBACK, LifecycleAction.DISABLE}
    ),
    PluginState.UNINSTALLED: frozenset(),
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """One entry in the installation's version ledger."""

    version: Version
    at: datetime

    def to_dict(self) -> dict[str, str]:
        return {"version": str(self.version), "at": self.at.isoformat()}


@dataclass(frozen=True, slots=True)
class PluginInstallation:
    """The durable state of one installed plugin (pure value object)."""

    plugin_id: str
    version: Version
    state: PluginState
    failure_count: int = 0
    history: tuple[VersionRecord, ...] = ()
    updated_at: datetime = field(default_factory=_now)

    # -- predicates ----------------------------------------------------- #

    @property
    def is_active(self) -> bool:
        return self.state.is_active

    @property
    def previous_version(self) -> Version | None:
        """The version to roll back to: the latest history entry != current."""
        for record in reversed(self.history):
            if record.version != self.version:
                return record.version
        return None

    def can(self, action: LifecycleAction) -> bool:
        """True when ``action`` is legal from the current state."""
        return action in _TRANSITIONS.get(self.state, frozenset())

    # -- transitions (return a NEW installation, never mutate) --------- #

    def _require(self, action: LifecycleAction) -> None:
        if not self.can(action):
            raise LifecycleError(
                f"cannot {action.value} plugin {self.plugin_id!r} from state {self.state.value!r}"
            )

    def enable(self) -> PluginInstallation:
        """Activate the plugin. From INSTALLED/DISABLED/QUARANTINED/UPGRADING."""
        self._require(LifecycleAction.ENABLE)
        history = _append_version(self.history, self.version)
        return replace(
            self,
            state=PluginState.ENABLED,
            failure_count=0,
            history=history,
            updated_at=_now(),
        )

    def disable(self) -> PluginInstallation:
        """Deactivate the plugin (hooks unregistered) without uninstalling."""
        self._require(LifecycleAction.DISABLE)
        return replace(self, state=PluginState.DISABLED, updated_at=_now())

    def begin_upgrade(self, new_version: Version) -> PluginInstallation:
        """Enter UPGRADING at ``new_version`` (must be a real version change).

        The store re-resolves dependencies for ``new_version`` while in this
        state, then calls :meth:`enable` to commit or :meth:`rollback` to revert.
        """
        self._require(LifecycleAction.UPGRADE)
        if new_version == self.version:
            raise LifecycleError(
                f"upgrade target {new_version} equals current version of {self.plugin_id!r}"
            )
        return replace(
            self,
            version=new_version,
            state=PluginState.UPGRADING,
            updated_at=_now(),
        )

    def rollback(self, *, to: Version | None = None) -> PluginInstallation:
        """Revert to ``to`` (or the previous good version) and re-enable.

        Used after a failed upgrade or a bad release. The target must appear in
        the version ledger (you can only roll back to a version this install has
        run before).
        """
        self._require(LifecycleAction.ROLLBACK)
        target = to or self.previous_version
        if target is None:
            raise LifecycleError(f"no previous version to roll back to for {self.plugin_id!r}")
        if not any(r.version == target for r in self.history):
            raise LifecycleError(
                f"rollback target {target} is not in the version history of {self.plugin_id!r}"
            )
        history = _append_version(self.history, target)
        return replace(
            self,
            version=target,
            state=PluginState.ENABLED,
            failure_count=0,
            history=history,
            updated_at=_now(),
        )

    def record_failure(self, *, quarantine_threshold: int = 5) -> PluginInstallation:
        """Increment the runtime failure counter; quarantine past the threshold.

        The host calls this when a hook raises in production. Once the count
        crosses ``quarantine_threshold`` an ENABLED plugin trips the breaker into
        QUARANTINED so a flapping plugin stops being dispatched.
        """
        count = self.failure_count + 1
        if self.state is PluginState.ENABLED and count >= quarantine_threshold:
            return replace(
                self,
                state=PluginState.QUARANTINED,
                failure_count=count,
                updated_at=_now(),
            )
        return replace(self, failure_count=count, updated_at=_now())

    def uninstall(self) -> PluginInstallation:
        """Move to the terminal UNINSTALLED state (history retained for audit)."""
        self._require(LifecycleAction.UNINSTALL)
        return replace(self, state=PluginState.UNINSTALLED, updated_at=_now())


def install(plugin_id: str, version: Version) -> PluginInstallation:
    """Create a fresh INSTALLED installation (the ``INSTALL`` transition)."""
    return PluginInstallation(
        plugin_id=plugin_id,
        version=version,
        state=PluginState.INSTALLED,
        history=(),
        updated_at=_now(),
    )


def _append_version(
    history: tuple[VersionRecord, ...], version: Version
) -> tuple[VersionRecord, ...]:
    """Append ``version`` to the ledger unless it is already the last entry."""
    if history and history[-1].version == version:
        return history
    return (*history, VersionRecord(version=version, at=_now()))


__all__ = [
    "LifecycleAction",
    "PluginInstallation",
    "PluginState",
    "VersionRecord",
    "install",
]
