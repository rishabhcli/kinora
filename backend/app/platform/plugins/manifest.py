"""The plugin manifest — the declarative contract a plugin ships with.

A manifest is the *single source of truth* about what a plugin is and what it is
allowed to touch. It is pure data: parsing/validation never executes plugin
code. The manifest declares, at minimum:

* identity — ``id`` (reverse-DNS-ish slug), ``version`` (SemVer), ``name``,
  publisher;
* the **requested capabilities** (least-privilege scopes, §capability model);
* the **hooks** it registers (which extension points, §hooks);
* its **dependencies** — other plugins by id + version range, and the host API
  version it targets (``api_version``);
* its **resource-limit requests** (clamped by the host ceiling);
* its **import allowlist** — the exact stdlib/3p modules its code may import
  (on top of the host's base allowlist).

Validation enforces the invariants that make a manifest *safe to act on*:
known capabilities only, unique hook ids, parseable versions/ranges, and a
``net.fetch`` capability whenever a webhook/outbound hook is declared. A
manifest that passes :meth:`PluginManifest.parse` is structurally trustworthy;
whether to *grant* what it asks for is a separate policy decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.platform.plugins.capabilities import GrantSet, RiskTier, risk_of
from app.platform.plugins.errors import PluginValidationError
from app.platform.plugins.hooks import ExtensionPoint, HookSpec
from app.platform.plugins.limits import ResourceLimits
from app.platform.plugins.version import Version, VersionRange

#: The host extension API version this build implements. A manifest declares the
#: range it supports via ``api_version``; an out-of-range plugin is rejected at
#: install so a host upgrade that breaks the contract surfaces immediately.
HOST_API_VERSION = Version(1, 0, 0)

#: A plugin id is a dotted, lowercase, reverse-DNS-ish slug (``com.acme.tone``).
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$")


@dataclass(frozen=True, slots=True)
class Dependency:
    """A declared dependency on another plugin by id + version range."""

    plugin_id: str
    range: VersionRange
    optional: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Dependency:
        if not isinstance(data, dict):
            raise PluginValidationError("dependency must be an object")
        pid = data.get("plugin_id") or data.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise PluginValidationError(f"invalid dependency plugin id: {pid!r}")
        rng = data.get("range", "*")
        if not isinstance(rng, str):
            raise PluginValidationError("dependency range must be a string")
        return cls(
            plugin_id=pid,
            range=VersionRange.parse(rng),
            optional=bool(data.get("optional", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"plugin_id": self.plugin_id, "range": str(self.range), "optional": self.optional}


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A fully-validated plugin descriptor."""

    id: str
    version: Version
    name: str
    capabilities: GrantSet
    hooks: tuple[HookSpec, ...]
    dependencies: tuple[Dependency, ...] = ()
    api_version: VersionRange = field(default_factory=lambda: VersionRange.parse(">=1.0.0,<2.0.0"))
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    import_allowlist: frozenset[str] = field(default_factory=frozenset)
    publisher: str = ""
    description: str = ""
    homepage: str = ""
    entry_module: str = "plugin"

    # ------------------------------------------------------------------ #
    # Parsing / validation
    # ------------------------------------------------------------------ #

    @classmethod
    def parse(cls, data: dict[str, Any]) -> PluginManifest:
        """Validate + build a manifest from a raw dict (the wire/db form)."""
        if not isinstance(data, dict):
            raise PluginValidationError("manifest must be an object")

        pid = data.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise PluginValidationError(f"manifest id must be a reverse-DNS slug (got {pid!r})")

        version = Version.parse(_require_str(data, "version"))
        name = _require_str(data, "name")

        capabilities = GrantSet.from_iterable(data.get("capabilities", ()))

        raw_hooks = data.get("hooks", ())
        if not isinstance(raw_hooks, (list, tuple)):
            raise PluginValidationError("hooks must be a list")
        hooks = tuple(HookSpec.from_dict(h) for h in raw_hooks)
        _validate_unique_hook_ids(hooks)

        deps = tuple(Dependency.from_dict(d) for d in data.get("dependencies", ()))
        _validate_no_self_dependency(pid, deps)

        api_range = VersionRange.parse(str(data.get("api_version", ">=1.0.0,<2.0.0")))
        limits = ResourceLimits.from_dict(data.get("limits"))

        allow = data.get("import_allowlist", ())
        if not isinstance(allow, (list, tuple)):
            raise PluginValidationError("import_allowlist must be a list of module names")
        import_allowlist = frozenset(str(m) for m in allow)
        _validate_module_names(import_allowlist)

        manifest = cls(
            id=pid,
            version=version,
            name=name,
            capabilities=capabilities,
            hooks=hooks,
            dependencies=deps,
            api_version=api_range,
            limits=limits,
            import_allowlist=import_allowlist,
            publisher=str(data.get("publisher", "")),
            description=str(data.get("description", "")),
            homepage=str(data.get("homepage", "")),
            entry_module=str(data.get("entry_module", "plugin")),
        )
        manifest._check_capability_coherence()
        return manifest

    def _check_capability_coherence(self) -> None:
        """Cross-field rule: outbound hooks must request the network capability."""
        declares_outbound = any(h.point is ExtensionPoint.WEBHOOK_ACTION for h in self.hooks)
        if declares_outbound and not self.capabilities.permits("net.fetch"):
            raise PluginValidationError("a webhook.action hook requires the 'net.fetch' capability")

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def ref(self) -> str:
        """The canonical ``id@version`` reference."""
        return f"{self.id}@{self.version}"

    @property
    def max_risk(self) -> RiskTier:
        """The highest risk tier across requested capabilities."""
        return self.capabilities.max_risk

    @property
    def requires_review(self) -> bool:
        """True when any requested capability is HIGH risk (manual review gate)."""
        return any(risk_of(c) is RiskTier.HIGH for c in self.capabilities.grants)

    def hooks_for(self, point: ExtensionPoint) -> tuple[HookSpec, ...]:
        """The declared hooks at ``point``, ordered by priority then id."""
        matching = [h for h in self.hooks if h.point is point]
        return tuple(sorted(matching, key=lambda h: (h.priority, h.id)))

    def supports_host(self, host_version: Version = HOST_API_VERSION) -> bool:
        """True when this manifest targets a host API range that includes ``host_version``."""
        return self.api_version.matches(host_version)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": str(self.version),
            "name": self.name,
            "capabilities": list(self.capabilities.to_sorted()),
            "hooks": [h.to_dict() for h in self.hooks],
            "dependencies": [d.to_dict() for d in self.dependencies],
            "api_version": str(self.api_version),
            "limits": self.limits.to_dict(),
            "import_allowlist": sorted(self.import_allowlist),
            "publisher": self.publisher,
            "description": self.description,
            "homepage": self.homepage,
            "entry_module": self.entry_module,
        }


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

_MODULE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(f"manifest {key!r} must be a non-empty string")
    return value


def _validate_unique_hook_ids(hooks: tuple[HookSpec, ...]) -> None:
    seen: set[str] = set()
    for h in hooks:
        if h.id in seen:
            raise PluginValidationError(f"duplicate hook id: {h.id!r}")
        seen.add(h.id)


def _validate_no_self_dependency(pid: str, deps: tuple[Dependency, ...]) -> None:
    for d in deps:
        if d.plugin_id == pid:
            raise PluginValidationError(f"plugin {pid!r} cannot depend on itself")


def _validate_module_names(modules: frozenset[str]) -> None:
    for m in modules:
        if not _MODULE_RE.match(m):
            raise PluginValidationError(f"invalid module name in import_allowlist: {m!r}")


__all__ = ["HOST_API_VERSION", "Dependency", "PluginManifest"]
