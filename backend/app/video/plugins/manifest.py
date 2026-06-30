"""The plugin manifest — the declarative contract a video-provider plugin ships.

A manifest is the *single source of truth* about what a plugin is, what host
versions it works with, what it can render, what it imports, and how to
instantiate it. It is pure data: parsing and validating a manifest never
executes plugin code, so a hostile manifest can at worst be *rejected*, never
*run*. The manifest declares:

* **identity** — ``id`` (reverse-DNS-ish slug), ``version`` (SemVer), ``name``,
  ``publisher``;
* **host compatibility** — ``kinora_api``, the range of plugin-API versions this
  plugin supports; discovery skips a plugin whose range excludes
  :data:`~app.video.plugins.contracts.PLUGIN_API_VERSION`;
* a **capability profile** — what the model can do
  (:class:`~app.video.plugins.contracts.CapabilityProfile`);
* a **config schema** — the shape (and secret-ness) of the plugin's config
  (:class:`~app.video.plugins.config_schema.ConfigSchema`);
* an **entry point** — ``module:attr`` naming the factory callable; and
* a sandbox profile — an ``import_allowlist`` (extra modules its code may import)
  and ``resource_limits`` (wall-time / host-calls / output budgets it requests,
  clamped by the host ceiling).

:meth:`PluginManifest.parse` enforces the invariants that make a manifest *safe
to act on*; whether to actually *grant + activate* what it asks for is a
separate, later decision (the conformance gate + the registry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.video.plugins.config_schema import ConfigSchema
from app.video.plugins.contracts import PLUGIN_API_VERSION, CapabilityProfile, RenderMode
from app.video.plugins.errors import ManifestError
from app.video.plugins.limits import ResourceLimits
from app.video.plugins.version import Version, VersionRange

#: A plugin id is a dotted, lowercase, reverse-DNS-ish slug (``com.acme.lumavid``).
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$")
#: ``module.path:attr`` — the entry-point form (importable module, attribute).
_ENTRY_RE = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*:[a-zA-Z_][a-zA-Z0-9_]*$"
)
_MODULE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")

#: The default compatibility range when a manifest omits ``kinora_api``: the
#: current MAJOR series. A plugin should pin this explicitly; the default is
#: deliberately conservative (this MAJOR only).
_DEFAULT_API_RANGE = ">=1.0.0,<2.0.0"


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """A ``module:attr`` reference to a plugin's factory callable."""

    module: str
    attr: str

    @classmethod
    def parse(cls, text: str) -> EntryPoint:
        if not isinstance(text, str) or not _ENTRY_RE.match(text.strip()):
            raise ManifestError(f"entry_point must be 'module.path:attr' (got {text!r})")
        module, attr = text.strip().split(":", 1)
        return cls(module=module, attr=attr)

    def __str__(self) -> str:
        return f"{self.module}:{self.attr}"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """A fully-validated video-provider plugin descriptor."""

    id: str
    version: Version
    name: str
    capabilities: CapabilityProfile
    entry_point: EntryPoint
    kinora_api: VersionRange = field(default_factory=lambda: VersionRange.parse(_DEFAULT_API_RANGE))
    config_schema: ConfigSchema = field(default_factory=ConfigSchema)
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    import_allowlist: frozenset[str] = field(default_factory=frozenset)
    publisher: str = ""
    description: str = ""
    homepage: str = ""

    # ------------------------------------------------------------------ #
    # Parsing / validation
    # ------------------------------------------------------------------ #

    @classmethod
    def parse(cls, data: dict[str, Any]) -> PluginManifest:
        """Validate + build a manifest from a raw dict (the descriptor/wire form)."""
        if not isinstance(data, dict):
            raise ManifestError("manifest must be an object")

        pid = data.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise ManifestError(f"manifest id must be a reverse-DNS slug (got {pid!r})")

        version = Version.parse(_require_str(data, "version"))
        name = _require_str(data, "name")
        entry_point = EntryPoint.parse(_require_str(data, "entry_point"))

        capabilities = _parse_capabilities(data.get("capabilities"))
        kinora_api = VersionRange.parse(str(data.get("kinora_api", _DEFAULT_API_RANGE)))
        config_schema = ConfigSchema.from_iterable(data.get("config_schema"))
        resource_limits = ResourceLimits.from_dict(data.get("resource_limits"))

        allow = data.get("import_allowlist", ())
        if not isinstance(allow, (list, tuple)):
            raise ManifestError("import_allowlist must be a list of module names")
        import_allowlist = frozenset(str(m) for m in allow)
        _validate_module_names(import_allowlist)

        return cls(
            id=pid,
            version=version,
            name=name,
            capabilities=capabilities,
            entry_point=entry_point,
            kinora_api=kinora_api,
            config_schema=config_schema,
            resource_limits=resource_limits,
            import_allowlist=import_allowlist,
            publisher=str(data.get("publisher", "")),
            description=str(data.get("description", "")),
            homepage=str(data.get("homepage", "")),
        )

    # ------------------------------------------------------------------ #
    # Derived properties / compatibility
    # ------------------------------------------------------------------ #

    @property
    def ref(self) -> str:
        """The canonical ``id@version`` reference."""
        return f"{self.id}@{self.version}"

    def is_compatible_with(self, host_api: str = PLUGIN_API_VERSION) -> bool:
        """True when this plugin's ``kinora_api`` range includes ``host_api``.

        This is the version-compat gate: an incompatible plugin is skipped at
        discovery (never loaded), so a host ABI bump cleanly excludes plugins
        that predate it instead of crashing when they hit a changed contract.
        """
        return self.kinora_api.matches(Version.parse(host_api))

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": str(self.version),
            "name": self.name,
            "entry_point": str(self.entry_point),
            "kinora_api": str(self.kinora_api),
            "capabilities": {
                "modes": sorted(m.value for m in self.capabilities.modes),
                "resolutions": sorted(self.capabilities.resolutions),
                "min_duration_s": self.capabilities.min_duration_s,
                "max_duration_s": self.capabilities.max_duration_s,
                "supports_negative_prompt": self.capabilities.supports_negative_prompt,
                "supports_seed": self.capabilities.supports_seed,
                "supports_audio": self.capabilities.supports_audio,
                "max_reference_images": self.capabilities.max_reference_images,
            },
            "config_schema": self.config_schema.to_list(),
            "resource_limits": self.resource_limits.to_dict(),
            "import_allowlist": sorted(self.import_allowlist),
            "publisher": self.publisher,
            "description": self.description,
            "homepage": self.homepage,
        }


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"manifest {key!r} must be a non-empty string")
    return value


def _parse_capabilities(raw: Any) -> CapabilityProfile:
    """Build + validate the capability profile from the manifest's nested object."""
    if not isinstance(raw, dict):
        raise ManifestError("manifest 'capabilities' must be an object")
    raw_modes = raw.get("modes")
    if not isinstance(raw_modes, (list, tuple)) or not raw_modes:
        raise ManifestError("capabilities.modes must be a non-empty list")
    try:
        modes = frozenset(RenderMode(str(m)) for m in raw_modes)
    except ValueError as exc:
        raise ManifestError(f"capabilities.modes has an unknown mode: {exc}") from exc
    payload: dict[str, Any] = {"modes": modes}
    for key in (
        "resolutions",
        "min_duration_s",
        "max_duration_s",
        "supports_negative_prompt",
        "supports_seed",
        "supports_audio",
        "max_reference_images",
    ):
        if key in raw:
            payload[key] = raw[key]
    if "resolutions" in payload:
        res = payload["resolutions"]
        if not isinstance(res, (list, tuple)):
            raise ManifestError("capabilities.resolutions must be a list")
        payload["resolutions"] = frozenset(str(r) for r in res)
    try:
        return CapabilityProfile(**payload)
    except Exception as exc:  # noqa: BLE001 - normalize pydantic error to ManifestError
        raise ManifestError(f"invalid capability profile: {exc}") from exc


def _validate_module_names(modules: frozenset[str]) -> None:
    for m in modules:
        if not _MODULE_RE.match(m):
            raise ManifestError(f"invalid module name in import_allowlist: {m!r}")


__all__ = ["EntryPoint", "PluginManifest"]
