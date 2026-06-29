"""Typed error hierarchy for the plugin/extension platform.

The errors split along the two surfaces of the platform:

* **Authoring / lifecycle / registry** errors (``PluginValidationError``,
  ``PluginNotFoundError``, ``DependencyResolutionError``,
  ``LifecycleError``, ``SignatureError``) — raised when a *write* would produce
  an invalid manifest, an unsatisfiable dependency set, an illegal state
  transition, or a failed signature check. "Reject the bad write" is the correct
  behaviour here.

* **Sandbox / runtime** errors (``SandboxError`` and its subclasses) — raised
  when *executing* plugin code violates the sandbox contract: a denied
  capability, an exhausted resource budget, a forbidden import, or an
  uncaught exception inside the plugin. These are the security-relevant errors
  the deterministic sandbox tests assert on.

Every error carries a stable ``code`` so the API layer can map it to a JSON
error body without string matching.
"""

from __future__ import annotations


class PluginError(Exception):
    """Base class for every plugin-platform error."""

    #: Stable machine-readable error code (overridden per subclass).
    code: str = "plugin_error"


# --------------------------------------------------------------------------- #
# Authoring / persistence / lifecycle
# --------------------------------------------------------------------------- #


class PluginValidationError(PluginError):
    """A manifest / capability grant / permission set is structurally invalid."""

    code = "plugin_invalid"


class PluginNotFoundError(PluginError):
    """A plugin id/version was requested from the registry but does not exist."""

    code = "plugin_not_found"


class DependencyResolutionError(PluginError):
    """A plugin's declared dependencies cannot be satisfied (missing/cycle/conflict)."""

    code = "plugin_dependency_unresolved"


class LifecycleError(PluginError):
    """An install/enable/upgrade/rollback transition is illegal from the current state."""

    code = "plugin_lifecycle"


class SignatureError(PluginError):
    """A plugin artifact's signature is missing, malformed, or does not verify."""

    code = "plugin_signature_invalid"


class RegistryError(PluginError):
    """A marketplace/registry operation failed (publish/review/rating conflict)."""

    code = "plugin_registry"


# --------------------------------------------------------------------------- #
# Sandbox / runtime — the security-relevant errors
# --------------------------------------------------------------------------- #


class SandboxError(PluginError):
    """Base class for any violation raised while *executing* plugin code."""

    code = "sandbox_error"


class CapabilityDeniedError(SandboxError):
    """The plugin attempted a host-API call it was never granted the scope for.

    This is the central least-privilege guarantee: the broker raises this
    *before* the underlying host operation runs, so a denied capability has no
    side effect. Carries the offending ``capability`` for audit/telemetry.
    """

    code = "capability_denied"

    def __init__(self, message: str, *, capability: str | None = None) -> None:
        super().__init__(message)
        self.capability = capability


class ResourceLimitError(SandboxError):
    """A plugin exceeded a declared resource budget (CPU/wall/memory/calls/output)."""

    code = "resource_limit_exceeded"

    def __init__(self, message: str, *, limit: str | None = None) -> None:
        super().__init__(message)
        #: Which limit tripped (``wall_time`` / ``memory`` / ``host_calls`` / ...).
        self.limit = limit


class ForbiddenImportError(SandboxError):
    """Plugin code tried to import a module outside the allowlist."""

    code = "forbidden_import"

    def __init__(self, message: str, *, module: str | None = None) -> None:
        super().__init__(message)
        self.module = module


class PluginRuntimeError(SandboxError):
    """The plugin raised an uncaught exception during execution.

    The original exception is preserved as ``__cause__`` and a sanitized
    representation is exposed via :attr:`original` for the host log (the raw
    traceback never crosses the trust boundary back to the host caller).
    """

    code = "plugin_runtime_error"

    def __init__(self, message: str, *, original: str | None = None) -> None:
        super().__init__(message)
        self.original = original


class HookError(PluginError):
    """A hook/extension-point registration or dispatch was misused."""

    code = "plugin_hook"


__all__ = [
    "CapabilityDeniedError",
    "DependencyResolutionError",
    "ForbiddenImportError",
    "HookError",
    "LifecycleError",
    "PluginError",
    "PluginNotFoundError",
    "PluginRuntimeError",
    "PluginValidationError",
    "RegistryError",
    "ResourceLimitError",
    "SandboxError",
    "SignatureError",
]
