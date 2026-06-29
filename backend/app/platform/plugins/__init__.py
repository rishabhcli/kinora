"""Kinora sandboxed plugin / extension platform (``app.platform.plugins``).

A self-contained extensibility platform that lets first- and third-party code
extend Kinora at four typed seams — ingest filters, custom agents, render
post-processors, and webhook actions — **without ambient authority**. The design
mirrors the rest of the platform layer: a *pure, deterministic core usable with
zero infrastructure*, wrapped by optional Postgres persistence (the registry),
a host-API broker, and an admin/marketplace API.

The pieces (see ``DESIGN.md`` for the full architecture):

* **Capability model** (:mod:`.capabilities`) — hierarchical dotted scopes,
  a closed catalog tagged by risk tier, and a deny-by-default :class:`GrantSet`.
* **Manifest** (:mod:`.manifest`) — the declarative, validated descriptor a
  plugin ships with (identity, requested capabilities, hooks, dependencies,
  resource-limit requests, import allowlist).
* **Sandbox runtime** (:mod:`.runtime`) — executes plugin code under a
  restricted import allowlist, resource budgets, and no ambient FS/network.
* **Host-API broker** (:mod:`.broker`) — the *only* way sandboxed code reaches
  host functionality; exposes exactly the granted capabilities.
* **Hooks** (:mod:`.hooks`) + **registry** (:mod:`.registry`) — the typed
  extension-point registry and deterministic dispatcher.
* **Lifecycle** (:mod:`.lifecycle`) + **resolver** (:mod:`.resolver`) —
  install/enable/upgrade/rollback state machine and dependency resolution.
* **Marketplace** (:mod:`.marketplace`) + **signing** (:mod:`.signing`) —
  publish / sign / verify / review / rate.
* **Service** (:mod:`.service`) + **store** (:mod:`.store`) — the orchestrating
  facade and async Postgres persistence.

Everything pure imports nothing from the network or DB; the persistence + broker
layers are additive and only constructed when the platform is enabled.
"""

from __future__ import annotations

from app.platform.plugins.broker import CallMeter, HostAPI, HostServices
from app.platform.plugins.capabilities import (
    CAPABILITY_CATALOG,
    EMPTY_GRANTS,
    Capability,
    CapabilitySpec,
    GrantSet,
    RiskTier,
    is_known_capability,
    risk_of,
)
from app.platform.plugins.errors import (
    CapabilityDeniedError,
    DependencyResolutionError,
    ForbiddenImportError,
    HookError,
    LifecycleError,
    PluginError,
    PluginNotFoundError,
    PluginRuntimeError,
    PluginValidationError,
    RegistryError,
    ResourceLimitError,
    SandboxError,
    SignatureError,
)
from app.platform.plugins.hooks import (
    EXTENSION_POINT_KIND,
    SUGGESTED_CAPABILITIES,
    ExtensionPoint,
    HookKind,
    HookSpec,
)
from app.platform.plugins.lifecycle import (
    LifecycleAction,
    PluginInstallation,
    PluginState,
    install,
)
from app.platform.plugins.limits import DEFAULT_CEILING, ResourceLimits
from app.platform.plugins.manifest import HOST_API_VERSION, Dependency, PluginManifest
from app.platform.plugins.marketplace import (
    CatalogListing,
    RatingStats,
    ReviewDecision,
    ReviewStatus,
)
from app.platform.plugins.registry import (
    DispatchReport,
    HookOutcome,
    HookRegistry,
    RegisteredHook,
)
from app.platform.plugins.resolver import (
    AvailablePlugin,
    DependencyResolver,
    ResolutionResult,
)
from app.platform.plugins.runtime import (
    BASE_IMPORT_ALLOWLIST,
    HOST_IMPORT_DENYLIST,
    InvocationResult,
    LoadedPlugin,
    PluginRuntime,
)
from app.platform.plugins.service import (
    InstallPlan,
    PluginPlatformConfig,
    PluginService,
    PluginUnitOfWork,
)
from app.platform.plugins.signing import (
    Signature,
    Signer,
    artifact_digest,
    verify_signature,
)
from app.platform.plugins.version import Version, VersionRange

__all__ = [
    "BASE_IMPORT_ALLOWLIST",
    "CAPABILITY_CATALOG",
    "DEFAULT_CEILING",
    "EMPTY_GRANTS",
    "EXTENSION_POINT_KIND",
    "HOST_API_VERSION",
    "HOST_IMPORT_DENYLIST",
    "SUGGESTED_CAPABILITIES",
    "AvailablePlugin",
    "CallMeter",
    "Capability",
    "CapabilityDeniedError",
    "CapabilitySpec",
    "CatalogListing",
    "Dependency",
    "DependencyResolutionError",
    "DependencyResolver",
    "DispatchReport",
    "ExtensionPoint",
    "ForbiddenImportError",
    "GrantSet",
    "HookError",
    "HookKind",
    "HookOutcome",
    "HookRegistry",
    "HookSpec",
    "HostAPI",
    "HostServices",
    "InstallPlan",
    "InvocationResult",
    "LifecycleAction",
    "LifecycleError",
    "LoadedPlugin",
    "PluginError",
    "PluginInstallation",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginPlatformConfig",
    "PluginRuntime",
    "PluginRuntimeError",
    "PluginService",
    "PluginState",
    "PluginUnitOfWork",
    "PluginValidationError",
    "RatingStats",
    "RegisteredHook",
    "RegistryError",
    "ResolutionResult",
    "ResourceLimitError",
    "ResourceLimits",
    "ReviewDecision",
    "ReviewStatus",
    "RiskTier",
    "SandboxError",
    "Signature",
    "Signer",
    "SignatureError",
    "Version",
    "VersionRange",
    "artifact_digest",
    "install",
    "is_known_capability",
    "risk_of",
    "verify_signature",
]
