"""Kinora video-provider **plugin SDK** — add a new model without forking Kinora.

The mandate is "video generation works with ANY model." This package is the
extensibility surface that makes that *open-ended*: a third party packages a
brand-new video model as an installable plugin, and Kinora discovers, validates,
sandboxes, and routes to it **with no change to Kinora's source**. It is the
counterpart to the in-tree universal-provider abstraction — that lets the *team*
add a provider; this lets *anyone* add one.

A plugin travels a strict admission pipeline; each stage is failure-isolated so a
single bad plugin never destabilizes the host:

1. **Discovery** (:mod:`~app.video.plugins.discovery`) — find candidates from
   Python entry points (``importlib.metadata``, group ``kinora.video_plugins``)
   and/or a directory of ``*.plugin.json`` descriptors. Reads *data only*;
   incompatible (``kinora_api`` mismatch) or malformed candidates are recorded
   as skips, never raised.
2. **Manifest + version-compat** (:mod:`~app.video.plugins.manifest`,
   :mod:`~app.video.plugins.version`) — the declarative
   :class:`~app.video.plugins.manifest.PluginManifest` (id, SemVer version,
   ``kinora_api`` compatibility range, capability profile, config schema, entry
   point, sandbox profile). Pure validation; never runs plugin code.
3. **Loading + sandbox** (:mod:`~app.video.plugins.loader`,
   :mod:`~app.video.plugins.sandbox`) — config is validated against the manifest
   schema, then the factory is instantiated behind a capability-scoped
   :class:`~app.video.plugins.sandbox.HostHandle` with **no ambient credentials**,
   a wall-time/host-call/output budget, and full error containment.
4. **Conformance gate** (:mod:`~app.video.plugins.conformance`) — a freshly
   loaded plugin is driven through a LOCAL conformance contract *through the
   sandbox* before activation. Pass ⇒ ACTIVE; fail ⇒ **QUARANTINED**.
5. **Registry** (:mod:`~app.video.plugins.registry`) — lifecycle
   (active/disabled/quarantined), per-plugin health, and the routable set.

:class:`~app.video.plugins.service.VideoPluginService` composes all of the above
into one ``admit`` call. :func:`~app.video.plugins.scaffold.create_plugin_template`
generates a working starter plugin for an author.

**Final-round note.** Rounds 1 & 2 are not merged, so this SDK does not import
their universal-provider / conformance-suite packages. It mirrors them with
minimal LOCAL contracts (:class:`~app.video.plugins.contracts.VideoProviderPlugin`,
:class:`~app.video.plugins.conformance.ConformanceCase`); the orchestrator wires
the real ones at final integration via thin adapters whose field names already
line up with ``app.providers.types``.
"""

from __future__ import annotations

from app.video.plugins.config_schema import ConfigField, ConfigSchema
from app.video.plugins.conformance import (
    ConformanceCase,
    ConformanceHarness,
    ConformanceReport,
)
from app.video.plugins.contracts import (
    PLUGIN_API_VERSION,
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoProviderPlugin,
    VideoRequest,
)
from app.video.plugins.discovery import (
    DiscoveryResult,
    PluginDiscoverer,
    SkippedPlugin,
)
from app.video.plugins.errors import (
    AmbientCredentialError,
    CapabilityDeniedError,
    ConfigSchemaError,
    ConformanceError,
    DiscoveryError,
    ForbiddenImportError,
    IncompatiblePluginError,
    ManifestError,
    PluginNotFoundError,
    PluginRuntimeError,
    RegistryStateError,
    ResourceLimitError,
    SandboxError,
    VideoPluginError,
)
from app.video.plugins.limits import HOST_CEILING, ResourceLimits
from app.video.plugins.loader import LoadedPlugin, PluginLoader
from app.video.plugins.manifest import EntryPoint, PluginManifest
from app.video.plugins.registry import (
    HealthRecord,
    PluginRegistry,
    PluginState,
    RegistryEntry,
)
from app.video.plugins.sandbox import (
    KNOWN_CAPABILITIES,
    CapabilityGrant,
    HostHandle,
    HostServices,
    Sandbox,
)
from app.video.plugins.scaffold import (
    ScaffoldedPlugin,
    create_plugin_template,
    write_plugin_template,
)
from app.video.plugins.service import (
    AdmissionOutcome,
    AdmissionResult,
    VideoPluginService,
)
from app.video.plugins.version import Version, VersionRange

__all__ = [
    "HOST_CEILING",
    "KNOWN_CAPABILITIES",
    "PLUGIN_API_VERSION",
    "AdmissionOutcome",
    "AdmissionResult",
    "AmbientCredentialError",
    "CapabilityDeniedError",
    "CapabilityGrant",
    "CapabilityProfile",
    "ConfigField",
    "ConfigSchema",
    "ConfigSchemaError",
    "ConformanceCase",
    "ConformanceError",
    "ConformanceHarness",
    "ConformanceReport",
    "DiscoveryError",
    "DiscoveryResult",
    "EntryPoint",
    "ForbiddenImportError",
    "HealthRecord",
    "HostHandle",
    "HostServices",
    "IncompatiblePluginError",
    "LoadedPlugin",
    "ManifestError",
    "PluginDiscoverer",
    "PluginLoader",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginRegistry",
    "PluginRuntimeError",
    "PluginState",
    "ProbeResult",
    "RegistryEntry",
    "RegistryStateError",
    "RenderMode",
    "ResourceLimitError",
    "ResourceLimits",
    "Sandbox",
    "SandboxError",
    "ScaffoldedPlugin",
    "SkippedPlugin",
    "Version",
    "VersionRange",
    "VideoArtifact",
    "VideoPluginError",
    "VideoPluginService",
    "VideoProviderPlugin",
    "VideoRequest",
    "create_plugin_template",
    "write_plugin_template",
]
