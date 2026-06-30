"""Plugin loading — turn a discovered manifest into a sandboxed, live plugin.

Discovery produced *data* (a validated, compatible manifest). Loading is where
third-party *code* first runs, so every step is gated:

1. **Config validation.** The supplied config payload is checked against the
   manifest's declared :class:`~app.video.plugins.config_schema.ConfigSchema`
   *before* the factory is resolved — a bad config never reaches plugin code.
2. **Secret extraction.** The schema's secret-flagged fields are split out and
   injected into the sandbox handle's ``secrets`` map; they are the *only*
   credentials the plugin will ever see (no-ambient-creds).
3. **Entry-point resolution.** The ``module:attr`` factory is imported. By
   default this uses :func:`importlib.import_module`; tests inject a resolver so
   no real distribution is needed.
4. **Sandboxed instantiation.** The factory is called with the validated config
   and a capability-scoped :class:`~app.video.plugins.sandbox.HostHandle`, under
   error containment — a constructor that explodes is a typed, contained failure.

A :class:`LoadedPlugin` bundles the live plugin object with the sandbox that
drives it, ready for the conformance gate and (on pass) the registry. The loader
does *not* itself decide activation — it produces a sandboxed plugin; the
registry/service runs conformance and flips the state.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.video.plugins.contracts import VideoProviderPlugin
from app.video.plugins.errors import PluginRuntimeError
from app.video.plugins.manifest import PluginManifest
from app.video.plugins.sandbox import (
    CapabilityGrant,
    HostServices,
    Sandbox,
)

logger = get_logger("app.video.plugins.loader")

#: Resolves a ``module:attr`` entry point to the factory callable. Injectable so
#: tests provide a factory directly without an installed distribution.
EntryPointResolver = Callable[[str, str], Callable[..., VideoProviderPlugin]]


@dataclass(slots=True)
class LoadedPlugin:
    """A live, sandboxed plugin ready for conformance + activation."""

    manifest: PluginManifest
    instance: VideoProviderPlugin
    sandbox: Sandbox
    #: The validated, schema-normalized config (secrets still present here; the
    #: registry/log path redacts via ``manifest.config_schema.redact``).
    config: dict[str, Any]

    @property
    def ref(self) -> str:
        return self.manifest.ref


class PluginLoader:
    """Loads a discovered manifest into a sandboxed plugin instance."""

    def __init__(self, *, resolver: EntryPointResolver | None = None) -> None:
        self._resolver = resolver or _default_resolver

    def load(
        self,
        manifest: PluginManifest,
        *,
        config: dict[str, Any] | None = None,
        grant: CapabilityGrant,
        services: HostServices | None = None,
    ) -> LoadedPlugin:
        """Validate config, resolve the factory, and instantiate under the sandbox.

        Args:
            manifest: the discovered, compatible manifest.
            config: the raw config payload (validated against the manifest schema).
            grant: the capabilities the host is willing to grant this plugin.
            services: the host-side service implementations (fetch/log/usage). A
                no-spend stub is supplied by the caller for conformance; the real
                services are wired at integration. Secrets are filled in here from
                the validated config so the plugin sees *only* its declared secrets.
        """
        resolved_config = manifest.config_schema.validate(config)
        secret_fields = manifest.config_schema.secret_fields
        secrets = {
            name: str(value)
            for name, value in resolved_config.items()
            if name in secret_fields
        }

        base_services = services or HostServices()
        scoped_services = HostServices(
            fetch=base_services.fetch,
            log_sink=base_services.log_sink,
            usage_sink=base_services.usage_sink,
            secrets=secrets,
        )

        sandbox = Sandbox(
            plugin_id=manifest.id,
            grant=grant,
            services=scoped_services,
            limits=manifest.resource_limits,
        )
        handle, _meter = sandbox.make_handle()

        factory = self._resolve_factory(manifest)
        instance = Sandbox.instantiate(factory, config=resolved_config, host=handle)

        # The runtime profile must match what the manifest advertised; a mismatch
        # is caught again (with a clearer message) by the conformance gate, but we
        # fail fast here if the plugin object isn't even shaped like the contract.
        if getattr(instance, "capabilities", None) is None:
            raise PluginRuntimeError(
                f"plugin {manifest.ref} instance exposes no 'capabilities' profile"
            )

        logger.info(
            "plugin_loaded",
            plugin=manifest.ref,
            granted=sorted(grant.granted),
            config=manifest.config_schema.redact(resolved_config),
        )
        return LoadedPlugin(
            manifest=manifest,
            instance=instance,
            sandbox=sandbox,
            config=resolved_config,
        )

    def _resolve_factory(self, manifest: PluginManifest) -> Callable[..., VideoProviderPlugin]:
        ep = manifest.entry_point
        try:
            return self._resolver(ep.module, ep.attr)
        except PluginRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - contain any import/attr failure
            raise PluginRuntimeError(
                f"could not resolve entry point {ep} for {manifest.ref}: {type(exc).__name__}",
                original=repr(exc),
            ) from exc


def _default_resolver(module: str, attr: str) -> Callable[..., VideoProviderPlugin]:
    """Import ``module`` and fetch ``attr`` (production entry-point resolution)."""
    mod = importlib.import_module(module)
    target = getattr(mod, attr, None)
    if target is None or not callable(target):
        raise PluginRuntimeError(f"entry point {module}:{attr} is not a callable factory")
    return target


__all__ = ["EntryPointResolver", "LoadedPlugin", "PluginLoader"]
