"""The declarative video-provider catalog (the data model + a YAML/JSON loader).

The catalog is the *source of truth* for which video models exist, what each can
do, whether it is enabled, how much routing weight it carries, and where it sits
in the rollout. It is plain data — parsed once into validated pydantic models —
so it can be authored in a checked-in YAML file, overridden by an operator's
file, or built in-memory by a test. Nothing here renders or spends; loading the
catalog is the same with ``KINORA_LIVE_VIDEO`` on or off.

Shape:

* :class:`ProviderEntry` — one model: ``id``, :class:`ProviderKind`
  (frontier/open/gateway), a :class:`~app.video.registry.capabilities.CapabilityProfile`,
  an ``enabled`` flag, a non-negative routing ``weight``, a ``cost_tier`` *ref*
  (a free-form string key into a sibling cost table — we don't own pricing), and
  a :class:`RolloutState`.
* :class:`ProviderCatalog` — the validated collection (unique ids, ≥1 entry),
  with convenience accessors the registry layers on top of.

Two parse entry points — :func:`load_catalog_text` (raw YAML/JSON string) and
:func:`load_catalog_file` (a path) — and a :func:`default_catalog_path` pointing
at the checked-in :file:`providers.yaml` beside this module. A malformed catalog
raises :class:`CatalogError` with an actionable message rather than a bare
``ValidationError``, so a hot-reload that hits a typo fails loudly but safely.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.video.registry.capabilities import CapabilityProfile

#: The checked-in catalog filename (shipped beside this module).
CATALOG_FILENAME = "providers.yaml"


class CatalogError(ValueError):
    """A catalog file/text could not be parsed or failed validation.

    Raised with a human-actionable message (provider id, offending field) so a
    bad edit surfaces clearly during a hot-reload instead of a raw pydantic dump.
    """


class ProviderKind(StrEnum):
    """The class of a video provider, for policy + introspection grouping.

    * ``frontier`` — a hosted, top-tier proprietary model (e.g. a Wan quality id).
    * ``open`` — an open-weights model we (could) self-host.
    * ``gateway`` — an aggregator/router that fronts several upstreams.
    """

    FRONTIER = "frontier"
    OPEN = "open"
    GATEWAY = "gateway"


class RolloutState(StrEnum):
    """Where a provider sits in its lifecycle (drives routing eligibility).

    * ``ga`` — generally available; eligible for normal traffic.
    * ``canary`` — receiving a small, weighted slice (A/B, soak).
    * ``preview`` — early access; opt-in only, never default traffic.
    * ``deprecated`` — being retired; eligible only if nothing else can serve.
    * ``disabled`` — administratively off regardless of the ``enabled`` flag.
    """

    GA = "ga"
    CANARY = "canary"
    PREVIEW = "preview"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class ProviderEntry(BaseModel):
    """One declarative video-model entry in the catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Stable, unique provider/model id (the registry key).",
    )
    kind: ProviderKind = Field(..., description="frontier | open | gateway.")
    display_name: str = Field(
        "", description="Human-readable label for UIs (defaults to id)."
    )
    capabilities: CapabilityProfile = Field(
        ..., description="What this provider can render (modes/res/duration)."
    )
    enabled: bool = Field(
        True, description="Operator feature flag; off => never routable."
    )
    weight: float = Field(
        1.0,
        ge=0.0,
        description="Relative routing weight for the weighted picker (0 => never picked).",
    )
    cost_tier: str = Field(
        "standard",
        min_length=1,
        description="Free-form ref into a sibling cost table (we don't own pricing).",
    )
    rollout: RolloutState = Field(
        RolloutState.GA, description="Lifecycle / rollout state."
    )
    provider_backend: str = Field(
        "",
        description="Optional hint at the concrete transport (e.g. 'dashscope', 'minimax').",
    )
    tags: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Free-form labels for filtering/grouping in the introspection API.",
    )

    @field_validator("id")
    @classmethod
    def _strip_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("provider id must not be blank")
        return stripped

    @property
    def label(self) -> str:
        """``display_name`` if set, else the id (never blank)."""
        return self.display_name or self.id

    @property
    def is_routable(self) -> bool:
        """Eligible for *any* routing: enabled, positive weight, not off/disabled.

        ``preview`` and ``deprecated`` are routable in principle (the picker /
        capability query decides whether to actually send them traffic); only an
        ``enabled=False`` flag, a ``DISABLED`` rollout, or a zero weight make an
        entry categorically un-routable.
        """
        return (
            self.enabled
            and self.weight > 0.0
            and self.rollout is not RolloutState.DISABLED
        )


class ProviderCatalog(BaseModel):
    """A validated collection of :class:`ProviderEntry` (unique ids, ≥1 entry)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(1, ge=1, description="Catalog schema version (for migrations).")
    providers: tuple[ProviderEntry, ...] = Field(
        ..., min_length=1, description="The declared providers."
    )

    @field_validator("providers")
    @classmethod
    def _unique_ids(cls, value: tuple[ProviderEntry, ...]) -> tuple[ProviderEntry, ...]:
        seen: set[str] = set()
        dupes: list[str] = []
        for entry in value:
            if entry.id in seen:
                dupes.append(entry.id)
            seen.add(entry.id)
        if dupes:
            raise ValueError(f"duplicate provider id(s): {', '.join(sorted(set(dupes)))}")
        return value

    def by_id(self, provider_id: str) -> ProviderEntry | None:
        """The entry with this id, or ``None``."""
        for entry in self.providers:
            if entry.id == provider_id:
                return entry
        return None

    def ids(self) -> tuple[str, ...]:
        """Every provider id, in declaration order."""
        return tuple(entry.id for entry in self.providers)


def _coerce_catalog(raw: object, *, source: str) -> ProviderCatalog:
    """Validate an already-decoded mapping into a :class:`ProviderCatalog`."""
    if not isinstance(raw, dict):
        raise CatalogError(f"{source}: catalog root must be a mapping, got {type(raw).__name__}")
    try:
        return ProviderCatalog.model_validate(raw)
    except ValidationError as exc:  # surface a tidy, actionable message
        raise CatalogError(
            f"{source}: invalid catalog — {exc.error_count()} error(s): {exc}"
        ) from exc


def load_catalog_text(text: str, *, source: str = "<text>") -> ProviderCatalog:
    """Parse a YAML *or* JSON catalog string into a validated catalog.

    YAML is a superset of JSON, so a single ``yaml.safe_load`` handles both. An
    empty document or a non-mapping root is a :class:`CatalogError`.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{source}: not valid YAML/JSON — {exc}") from exc
    if raw is None:
        raise CatalogError(f"{source}: catalog is empty")
    return _coerce_catalog(raw, source=source)


def load_catalog_file(path: str | Path) -> ProviderCatalog:
    """Read and parse a catalog file (``.yaml`` / ``.yml`` / ``.json``).

    Raises:
        CatalogError: the file is missing, unreadable, or invalid.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog file not found: {p}") from exc
    except OSError as exc:  # pragma: no cover - rare I/O fault
        raise CatalogError(f"could not read catalog file {p}: {exc}") from exc
    return load_catalog_text(text, source=str(p))


def dump_catalog_json(catalog: ProviderCatalog) -> str:
    """Serialize a catalog back to canonical JSON (round-trips through the loader)."""
    return json.dumps(catalog.model_dump(mode="json"), indent=2, sort_keys=True)


def default_catalog_path() -> Path:
    """Absolute path to the checked-in :file:`providers.yaml` beside this module."""
    return Path(__file__).resolve().parent / CATALOG_FILENAME


def load_default_catalog() -> ProviderCatalog:
    """Load the checked-in default catalog (the shipped baseline)."""
    return load_catalog_file(default_catalog_path())


__all__ = [
    "CATALOG_FILENAME",
    "CatalogError",
    "ProviderCatalog",
    "ProviderEntry",
    "ProviderKind",
    "RolloutState",
    "default_catalog_path",
    "dump_catalog_json",
    "load_catalog_file",
    "load_catalog_text",
    "load_default_catalog",
]
