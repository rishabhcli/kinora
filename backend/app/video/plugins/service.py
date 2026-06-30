"""The SDK facade — discover, load, conformance-gate, and register, end to end.

This is the one object a host wires up. It composes the pieces into the full
admission pipeline a third-party plugin travels:

```
  discover (data only)
      │  compatible manifests          incompatible / broken → recorded skips
      ▼
  load (config-validated, sandboxed instantiation)
      │
      ▼
  conformance gate (run cases through the sandbox)
      │  PASS                          FAIL
      ▼                                  ▼
  registry: ACTIVE (routable)        registry: QUARANTINED (kept, never routed)
```

Every stage is failure-isolated: a manifest that won't load, a factory that
explodes, or a plugin that fails conformance becomes a recorded outcome in the
returned :class:`AdmissionResult` — never an exception that stops the rest of the
batch. The result is a host that boots with *all* the good plugins active and a
clean, inspectable record of why each bad one was held back.

The host supplies the capability grants (a policy decision — what is this plugin
allowed to touch?) and the host services. For conformance the service injects a
**no-spend stub** services object so the gate exercises behaviour shape without
ever hitting a paid API or leaking a real credential; the production services are
swapped in at final integration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.video.plugins.conformance import ConformanceHarness, ConformanceReport
from app.video.plugins.contracts import PLUGIN_API_VERSION
from app.video.plugins.discovery import (
    DiscoveredPlugin,
    PluginDiscoverer,
    SkippedPlugin,
)
from app.video.plugins.errors import (
    ConformanceError,
    VideoPluginError,
)
from app.video.plugins.loader import LoadedPlugin, PluginLoader
from app.video.plugins.manifest import PluginManifest
from app.video.plugins.registry import PluginRegistry, RegistryEntry
from app.video.plugins.sandbox import CapabilityGrant, HostServices, Sandbox

logger = get_logger("app.video.plugins.service")


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """What happened to one plugin candidate as it travelled the pipeline."""

    plugin_ref: str
    admitted: bool
    state: str
    detail: str = ""
    report: ConformanceReport | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """The aggregate outcome of one ``admit`` sweep."""

    activated: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    failed_to_load: tuple[AdmissionOutcome, ...] = ()
    skipped: tuple[SkippedPlugin, ...] = ()
    outcomes: tuple[AdmissionOutcome, ...] = ()


#: How the host grants capabilities to a plugin by id. A plugin not present in
#: the policy gets an empty grant (least privilege by default).
GrantPolicy = Mapping[str, CapabilityGrant]


@dataclass(slots=True)
class VideoPluginService:
    """Composes discovery + loading + the conformance gate + the registry."""

    registry: PluginRegistry = field(default_factory=PluginRegistry)
    discoverer: PluginDiscoverer = field(
        default_factory=lambda: PluginDiscoverer(host_api=PLUGIN_API_VERSION)
    )
    loader: PluginLoader = field(default_factory=PluginLoader)
    harness: ConformanceHarness = field(default_factory=ConformanceHarness)
    #: Services used *only* during the conformance gate — no real spend.
    conformance_services: HostServices = field(default_factory=HostServices)

    async def admit(
        self,
        *,
        directories: Iterable[Path] | None = None,
        include_entry_points: bool = True,
        entry_points: Iterable[Any] | None = None,
        grants: GrantPolicy | None = None,
        configs: Mapping[str, dict[str, Any]] | None = None,
        host_services: HostServices | None = None,
    ) -> AdmissionResult:
        """Run the full discover → load → conform → register pipeline.

        Args:
            directories / include_entry_points / entry_points: discovery sources.
            grants: per-plugin-id capability grants (host policy).
            configs: per-plugin-id config payloads (validated per manifest schema).
            host_services: the *production* services bound into an admitted
                plugin's registered sandbox. Conformance always runs against
                :attr:`conformance_services` regardless, so the gate never spends.
        """
        discovery = self.discoverer.discover(
            directories=directories,
            include_entry_points=include_entry_points,
            entry_points=entry_points,
        )

        activated: list[str] = []
        quarantined: list[str] = []
        failed: list[AdmissionOutcome] = []
        outcomes: list[AdmissionOutcome] = []

        for candidate in discovery.discovered:
            outcome = await self._admit_one(
                candidate,
                grants=grants or {},
                configs=configs or {},
                host_services=host_services,
            )
            outcomes.append(outcome)
            if outcome.state == "active":
                activated.append(outcome.plugin_ref)
            elif outcome.state == "quarantined":
                quarantined.append(outcome.plugin_ref)
            else:
                failed.append(outcome)

        return AdmissionResult(
            activated=tuple(activated),
            quarantined=tuple(quarantined),
            failed_to_load=tuple(failed),
            skipped=discovery.skipped,
            outcomes=tuple(outcomes),
        )

    async def _admit_one(
        self,
        candidate: DiscoveredPlugin,
        *,
        grants: GrantPolicy,
        configs: Mapping[str, dict[str, Any]],
        host_services: HostServices | None,
    ) -> AdmissionOutcome:
        manifest = candidate.manifest
        grant = grants.get(manifest.id, CapabilityGrant())
        config = configs.get(manifest.id)
        try:
            loaded = self.loader.load(
                manifest,
                config=config,
                grant=grant,
                services=host_services or self.conformance_services,
            )
        except VideoPluginError as exc:
            logger.warning("plugin_load_failed", plugin=manifest.ref, error=exc.code)
            return AdmissionOutcome(
                plugin_ref=manifest.ref,
                admitted=False,
                state="load_failed",
                detail=f"{exc.code}: {exc}",
            )

        report = await self._run_conformance(manifest, loaded)
        if report.passed:
            self.registry.register_active(loaded, report)
            return AdmissionOutcome(
                plugin_ref=manifest.ref, admitted=True, state="active", report=report
            )
        self.registry.register_quarantined(loaded, report)
        return AdmissionOutcome(
            plugin_ref=manifest.ref,
            admitted=False,
            state="quarantined",
            detail=f"failed conformance: {list(report.failures)}",
            report=report,
        )

    async def _run_conformance(
        self, manifest: PluginManifest, loaded: LoadedPlugin
    ) -> ConformanceReport:
        """Run the conformance contract through a no-spend sandbox.

        The gate runs against a fresh sandbox bound to :attr:`conformance_services`
        (a no-spend stub) rather than the plugin's registered production sandbox,
        so a conformance call can never trigger real fetch/usage even when the
        plugin was granted ``net.fetch`` for production. The plugin object itself
        is unchanged; only the host handle behind it differs.
        """
        gate_sandbox = Sandbox(
            plugin_id=manifest.id,
            grant=loaded.sandbox.grant,
            services=self._conformance_services_with_secrets(loaded),
            limits=manifest.resource_limits,
        )
        return await self.harness.run(
            plugin_ref=manifest.ref,
            plugin=loaded.instance,
            profile=manifest.capabilities,
            sandbox=gate_sandbox,
        )

    def _conformance_services_with_secrets(self, loaded: LoadedPlugin) -> HostServices:
        """No-spend conformance services that still expose the plugin's declared secrets.

        The gate keeps the plugin's config-declared secrets reachable (a plugin
        may legitimately read them in ``probe``) but routes fetch/usage to the
        no-spend stub, so conformance exercises behaviour shape without spend.
        """
        secret_fields = loaded.manifest.config_schema.secret_fields
        secrets = {k: str(v) for k, v in loaded.config.items() if k in secret_fields}
        return HostServices(
            fetch=self.conformance_services.fetch,
            log_sink=self.conformance_services.log_sink,
            usage_sink=self.conformance_services.usage_sink,
            secrets=secrets,
        )

    # -- convenience: route selection ------------------------------------- #

    def routable(self) -> tuple[RegistryEntry, ...]:
        """The currently routable (ACTIVE + healthy) plugins."""
        return self.registry.routable()

    def require_conformant(self, report: ConformanceReport) -> None:
        """Raise :class:`ConformanceError` if ``report`` did not pass (helper)."""
        if not report.passed:
            raise ConformanceError(
                f"{report.plugin_ref} failed conformance", failures=report.failures
            )


__all__ = ["AdmissionOutcome", "AdmissionResult", "GrantPolicy", "VideoPluginService"]
