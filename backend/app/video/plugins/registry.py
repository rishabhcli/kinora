"""The plugin registry — lifecycle, health, and quarantine.

The registry is the host's view of every plugin it knows about and the single
gate to *routability*: only an :data:`PluginState.ACTIVE` plugin is handed back
by :meth:`PluginRegistry.routable`. The state machine is small and total:

```
            register + conformance PASS        disable
   (new) ─────────────────────────────► ACTIVE ◄────────► DISABLED
            \\                              │ enable
             \\  conformance FAIL          │ (re-run conformance)
              ─────────────────────────► QUARANTINED
```

* **ACTIVE** — passed conformance, enabled; routable.
* **QUARANTINED** — failed the conformance gate; kept on record with its failure
  list but never routable. Re-running conformance can clear it.
* **DISABLED** — operator-disabled; conformant but intentionally held back.

Each entry also tracks **health** (a tiny success/failure tally with a last
error) so an operator can see a plugin that loads + conforms but then fails live
probes. Health never silently demotes an active plugin — that is an explicit
operator/scheduler policy decision — but it is the data such a policy reads.

The registry is pure in-memory state + transitions; it executes no plugin code
and does no I/O, so its lifecycle tests are exact. The async
discover→load→conform orchestration lives in :mod:`app.video.plugins.service`,
which calls into this registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.video.plugins.conformance import ConformanceReport
from app.video.plugins.contracts import CapabilityProfile
from app.video.plugins.errors import PluginNotFoundError, RegistryStateError
from app.video.plugins.loader import LoadedPlugin

logger = get_logger("app.video.plugins.registry")


class PluginState(StrEnum):
    """The lifecycle state of a registered plugin."""

    ACTIVE = "active"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


@dataclass(slots=True)
class HealthRecord:
    """A tiny health tally for one plugin (probe/generate successes vs failures)."""

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None

    def record_success(self) -> None:
        self.successes += 1
        self.consecutive_failures = 0
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        self.last_error = error

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def is_healthy(self) -> bool:
        """Healthy until it has failed at least twice in a row."""
        return self.consecutive_failures < 2


@dataclass(slots=True)
class RegistryEntry:
    """One plugin's full record in the registry."""

    loaded: LoadedPlugin
    state: PluginState
    health: HealthRecord = field(default_factory=HealthRecord)
    #: Names of the conformance cases that quarantined this plugin (if any).
    quarantine_failures: tuple[str, ...] = ()
    last_report: ConformanceReport | None = None

    @property
    def plugin_id(self) -> str:
        return self.loaded.manifest.id

    @property
    def ref(self) -> str:
        return self.loaded.ref

    @property
    def capabilities(self) -> CapabilityProfile:
        return self.loaded.manifest.capabilities

    @property
    def is_routable(self) -> bool:
        """Active *and* not currently failing health."""
        return self.state is PluginState.ACTIVE and self.health.is_healthy


class PluginRegistry:
    """In-memory registry of plugins with lifecycle + health, keyed by plugin id."""

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    # -- registration / conformance result ------------------------------- #

    def register_active(self, loaded: LoadedPlugin, report: ConformanceReport) -> RegistryEntry:
        """Admit a conformant plugin as ACTIVE (rejects a non-passing report)."""
        if not report.passed:
            raise RegistryStateError(
                f"refusing to activate {loaded.ref}: conformance not passed "
                f"(failures: {list(report.failures)})"
            )
        entry = RegistryEntry(loaded=loaded, state=PluginState.ACTIVE, last_report=report)
        self._entries[loaded.manifest.id] = entry
        logger.info("plugin_activated", plugin=loaded.ref)
        return entry

    def register_quarantined(
        self, loaded: LoadedPlugin, report: ConformanceReport
    ) -> RegistryEntry:
        """Record a non-conformant plugin as QUARANTINED (kept, not routable)."""
        entry = RegistryEntry(
            loaded=loaded,
            state=PluginState.QUARANTINED,
            quarantine_failures=report.failures,
            last_report=report,
        )
        self._entries[loaded.manifest.id] = entry
        logger.warning(
            "plugin_quarantined", plugin=loaded.ref, failures=list(report.failures)
        )
        return entry

    # -- lifecycle transitions ------------------------------------------- #

    def disable(self, plugin_id: str) -> RegistryEntry:
        """Disable an ACTIVE plugin (operator hold). Idempotent on DISABLED."""
        entry = self._get(plugin_id)
        if entry.state is PluginState.QUARANTINED:
            raise RegistryStateError(
                f"cannot disable quarantined plugin {plugin_id!r}; it is already non-routable"
            )
        entry.state = PluginState.DISABLED
        logger.info("plugin_disabled", plugin=entry.ref)
        return entry

    def enable(self, plugin_id: str, report: ConformanceReport | None = None) -> RegistryEntry:
        """Re-enable a DISABLED plugin, or clear a QUARANTINE on a passing report.

        Enabling a DISABLED plugin just flips it back to ACTIVE. Enabling a
        QUARANTINED plugin requires a *passing* conformance ``report`` — the gate
        is never bypassed; a plugin only leaves quarantine by proving conformance.
        """
        entry = self._get(plugin_id)
        if entry.state is PluginState.ACTIVE:
            return entry
        if entry.state is PluginState.QUARANTINED:
            if report is None or not report.passed:
                raise RegistryStateError(
                    f"cannot enable quarantined plugin {plugin_id!r} without a passing "
                    "conformance report"
                )
            entry.quarantine_failures = ()
            entry.last_report = report
        entry.state = PluginState.ACTIVE
        entry.health = HealthRecord()
        logger.info("plugin_enabled", plugin=entry.ref)
        return entry

    def remove(self, plugin_id: str) -> None:
        """Forget a plugin entirely (e.g. on uninstall)."""
        if plugin_id not in self._entries:
            raise PluginNotFoundError(f"no plugin registered with id {plugin_id!r}")
        del self._entries[plugin_id]
        logger.info("plugin_removed", plugin_id=plugin_id)

    # -- health ----------------------------------------------------------- #

    def record_health(self, plugin_id: str, *, ok: bool, error: str | None = None) -> None:
        """Record a live probe/generate outcome against a plugin's health."""
        entry = self._get(plugin_id)
        if ok:
            entry.health.record_success()
        else:
            entry.health.record_failure(error or "unknown error")

    # -- queries ---------------------------------------------------------- #

    def get(self, plugin_id: str) -> RegistryEntry:
        return self._get(plugin_id)

    def contains(self, plugin_id: str) -> bool:
        return plugin_id in self._entries

    def all_entries(self) -> tuple[RegistryEntry, ...]:
        return tuple(self._entries.values())

    def routable(self) -> tuple[RegistryEntry, ...]:
        """The plugins eligible to receive a render (ACTIVE + healthy)."""
        return tuple(e for e in self._entries.values() if e.is_routable)

    def quarantined(self) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self._entries.values() if e.state is PluginState.QUARANTINED)

    def supporting(self, mode: object) -> tuple[RegistryEntry, ...]:
        """Routable plugins whose capability profile advertises ``mode``."""
        return tuple(e for e in self.routable() if mode in e.capabilities.modes)

    def _get(self, plugin_id: str) -> RegistryEntry:
        entry = self._entries.get(plugin_id)
        if entry is None:
            raise PluginNotFoundError(f"no plugin registered with id {plugin_id!r}")
        return entry


__all__ = ["HealthRecord", "PluginRegistry", "PluginState", "RegistryEntry"]
