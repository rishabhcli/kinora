"""Typed error hierarchy for the video-provider plugin SDK.

The SDK has two trust surfaces, and the errors split along them:

* **Authoring / discovery / lifecycle** errors — raised when a *manifest* is
  structurally invalid, a config payload fails its declared schema, a plugin's
  declared compatibility range excludes this host, or a registry transition is
  illegal. The correct behaviour is "reject the bad input"; nothing has executed
  third-party code yet, so these are not security events.

* **Sandbox / activation** errors — raised when *loading or exercising* a
  third-party plugin crosses a boundary: an import outside the allow-list, an
  attempt to read an ambient credential, a wall-time/host-call budget overrun,
  an uncaught exception inside plugin code, or a failure of the conformance gate
  that keeps a broken plugin out of the active registry. These are the
  security-relevant errors the deterministic sandbox + quarantine tests assert.

Every error carries a stable, machine-readable ``code`` so an API layer can map
it to a structured error body without string matching, and so telemetry can
aggregate failures by class.
"""

from __future__ import annotations


class VideoPluginError(Exception):
    """Base class for every video-plugin SDK error."""

    #: Stable machine-readable error code (overridden per subclass).
    code: str = "video_plugin_error"


# --------------------------------------------------------------------------- #
# Authoring / discovery / lifecycle
# --------------------------------------------------------------------------- #


class ManifestError(VideoPluginError):
    """A :class:`~app.video.plugins.manifest.PluginManifest` is structurally invalid."""

    code = "video_plugin_manifest_invalid"


class ConfigSchemaError(VideoPluginError):
    """A plugin's config payload does not satisfy its declared config schema."""

    code = "video_plugin_config_invalid"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        #: The offending config field, when the failure is field-scoped.
        self.field = field


class IncompatiblePluginError(VideoPluginError):
    """A plugin targets a Kinora plugin-API range that excludes this host.

    Carried by discovery so an incompatible descriptor is *skipped gracefully*
    (logged + recorded as a skip) rather than crashing the host load.
    """

    code = "video_plugin_incompatible"

    def __init__(
        self,
        message: str,
        *,
        plugin_id: str | None = None,
        required: str | None = None,
        host: str | None = None,
    ) -> None:
        super().__init__(message)
        self.plugin_id = plugin_id
        #: The plugin's declared ``kinora_api`` compatibility range.
        self.required = required
        #: The host plugin-API version that fell outside it.
        self.host = host


class PluginNotFoundError(VideoPluginError):
    """A plugin id was requested from the registry but is not present."""

    code = "video_plugin_not_found"


class DiscoveryError(VideoPluginError):
    """A descriptor file / entry-point could not be read or parsed.

    Like :class:`IncompatiblePluginError`, discovery converts this into a
    *recorded skip* — one broken descriptor never aborts the whole sweep.
    """

    code = "video_plugin_discovery_failed"

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        #: The descriptor path / entry-point name the failure came from.
        self.source = source


class RegistryStateError(VideoPluginError):
    """An enable/disable/activate/quarantine transition is illegal from the current state."""

    code = "video_plugin_registry_state"


# --------------------------------------------------------------------------- #
# Conformance gate
# --------------------------------------------------------------------------- #


class ConformanceError(VideoPluginError):
    """A newly-loaded plugin failed the conformance contract and was quarantined.

    The host runs every freshly-loaded plugin through the conformance harness
    *before* activation; a plugin that fails any required case never enters the
    active set. The failing case names are exposed via :attr:`failures` for the
    quarantine record and operator triage.
    """

    code = "video_plugin_nonconformant"

    def __init__(self, message: str, *, failures: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        #: The names of the conformance cases the plugin failed.
        self.failures = failures


# --------------------------------------------------------------------------- #
# Sandbox / isolation — the security-relevant errors
# --------------------------------------------------------------------------- #


class SandboxError(VideoPluginError):
    """Base class for any violation raised while loading or executing plugin code."""

    code = "video_plugin_sandbox_error"


class CapabilityDeniedError(SandboxError):
    """The plugin reached for a capability its manifest never declared.

    This is the least-privilege guarantee of the SDK: the sandbox surfaces only
    the host services a plugin's :class:`~app.video.plugins.manifest.CapabilityProfile`
    grants, and an attempt to use anything else raises *before* the host
    operation runs. Carries the offending ``capability`` for audit/telemetry.
    """

    code = "video_plugin_capability_denied"

    def __init__(self, message: str, *, capability: str | None = None) -> None:
        super().__init__(message)
        self.capability = capability


class AmbientCredentialError(SandboxError):
    """The plugin tried to read an ambient credential / host secret.

    A plugin must receive every secret it needs explicitly through its validated
    config; reaching for process env, the host settings object, or a global
    credential store is a hard isolation breach.
    """

    code = "video_plugin_ambient_credential"

    def __init__(self, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class ResourceLimitError(SandboxError):
    """A plugin exceeded a declared resource budget (wall-time / host-calls / output)."""

    code = "video_plugin_resource_limit"

    def __init__(self, message: str, *, limit: str | None = None) -> None:
        super().__init__(message)
        #: Which budget tripped (``wall_time`` / ``host_calls`` / ``output_bytes``).
        self.limit = limit


class ForbiddenImportError(SandboxError):
    """Plugin code imported a module outside its effective allow-list."""

    code = "video_plugin_forbidden_import"

    def __init__(self, message: str, *, module: str | None = None) -> None:
        super().__init__(message)
        self.module = module


class PluginRuntimeError(SandboxError):
    """The plugin raised an uncaught exception that the sandbox contained.

    The original exception is preserved as ``__cause__``; a sanitized
    representation is exposed via :attr:`original` for the host log. The raw
    plugin traceback never crosses the boundary back to the host caller — error
    containment means a bad plugin surfaces as a typed, contained failure, never
    a host crash.
    """

    code = "video_plugin_runtime_error"

    def __init__(self, message: str, *, original: str | None = None) -> None:
        super().__init__(message)
        self.original = original


__all__ = [
    "AmbientCredentialError",
    "CapabilityDeniedError",
    "ConfigSchemaError",
    "ConformanceError",
    "DiscoveryError",
    "ForbiddenImportError",
    "IncompatiblePluginError",
    "ManifestError",
    "PluginNotFoundError",
    "PluginRuntimeError",
    "RegistryStateError",
    "ResourceLimitError",
    "SandboxError",
    "VideoPluginError",
]
