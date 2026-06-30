"""The runtime :class:`VideoProviderRegistry` over a declarative catalog.

The catalog (``catalog.py``) is the static source of truth; this is the *living*
view of it. The registry:

* loads a :class:`~app.video.registry.catalog.ProviderCatalog` and indexes it by
  id;
* supports :meth:`register` / :meth:`unregister` for runtime additions (a
  feature-flagged experiment, a test double) layered over the catalog;
* answers **capability queries** — "every routable provider that can do r2v at
  ≥720P for ≥8s" — via the local
  :class:`~app.video.registry.capabilities.CapabilityProfile.satisfies` predicate;
* carries **per-provider feature-flag overrides** (force-enable / force-disable a
  provider id at runtime without re-authoring the catalog), and a runtime
  **weight override** map for live canary tuning;
* exposes a deterministic **weighted picker** (:meth:`pick`) for canary / A-B
  routing built on ``picker.py``;
* supports a safe **hot-reload** (:meth:`reload`): a new catalog text is parsed
  and validated *before* anything is swapped, so a malformed reload raises
  :class:`~app.video.registry.catalog.CatalogError` and leaves the live registry
  untouched.

All state lives on the instance — no globals, no env reads, no I/O beyond the
optional file read in :meth:`reload`/:meth:`from_file`. That keeps it trivially
testable and lets the composition root own the single process-wide instance.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from app.core.logging import get_logger
from app.video.registry.capabilities import CapabilityProfile, Resolution, VideoMode
from app.video.registry.catalog import (
    ProviderCatalog,
    ProviderEntry,
    ProviderKind,
    RolloutState,
    load_catalog_file,
    load_catalog_text,
    load_default_catalog,
)
from app.video.registry.picker import (
    DEFAULT_SALT,
    WeightedCandidate,
    expected_distribution,
    pick_weighted,
)

logger = get_logger("app.video.registry")


class RegistrySnapshot(TypedDict):
    """A plain-data summary of the registry (for logs / introspection)."""

    version: int
    total: int
    routable: list[str]
    enabled_overrides: dict[str, bool]
    weight_overrides: dict[str, float]


class VideoProviderRegistry:
    """A thread-safe runtime registry of video providers over a catalog.

    Construct from a catalog directly, or via :meth:`from_file` /
    :meth:`from_default`. Lookups and queries are read-mostly; mutations
    (register/unregister/flags/weights/reload) take a lock so a concurrent
    request always sees a consistent snapshot.
    """

    def __init__(self, catalog: ProviderCatalog) -> None:
        self._lock = threading.RLock()
        self._catalog = catalog
        #: catalog entries + runtime registrations, keyed by id (insertion-ordered).
        self._entries: dict[str, ProviderEntry] = {e.id: e for e in catalog.providers}
        #: per-id force enable/disable override (None => use the entry's own flag).
        self._enabled_overrides: dict[str, bool] = {}
        #: per-id routing-weight override (None => use the entry's own weight).
        self._weight_overrides: dict[str, float] = {}

    # -- construction ----------------------------------------------------- #

    @classmethod
    def from_file(cls, path: str | Path) -> VideoProviderRegistry:
        """Build a registry from a YAML/JSON catalog file."""
        return cls(load_catalog_file(path))

    @classmethod
    def from_default(cls) -> VideoProviderRegistry:
        """Build a registry from the checked-in default catalog."""
        return cls(load_default_catalog())

    # -- lookup ----------------------------------------------------------- #

    def get(self, provider_id: str) -> ProviderEntry | None:
        """The entry for ``provider_id``, or ``None`` if unknown."""
        with self._lock:
            return self._entries.get(provider_id)

    def require(self, provider_id: str) -> ProviderEntry:
        """The entry for ``provider_id`` or raise :class:`KeyError`."""
        entry = self.get(provider_id)
        if entry is None:
            raise KeyError(f"no such video provider: {provider_id!r}")
        return entry

    def __contains__(self, provider_id: object) -> bool:
        with self._lock:
            return provider_id in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def all(self) -> list[ProviderEntry]:
        """Every known entry (catalog + runtime), in insertion order."""
        with self._lock:
            return list(self._entries.values())

    def ids(self) -> list[str]:
        """Every known provider id, in insertion order."""
        with self._lock:
            return list(self._entries)

    # -- effective state (flags + weight overrides applied) --------------- #

    def is_enabled(self, provider_id: str) -> bool:
        """Effective enabled state: the runtime override if set, else the entry flag."""
        with self._lock:
            entry = self._entries.get(provider_id)
            if entry is None:
                return False
            if provider_id in self._enabled_overrides:
                return self._enabled_overrides[provider_id]
            return entry.enabled

    def effective_weight(self, provider_id: str) -> float:
        """Effective routing weight: the override if set, else the entry weight.

        Returns ``0.0`` when the provider is not effectively enabled or its
        rollout is ``disabled`` — i.e. the weight the picker should actually see.
        """
        with self._lock:
            entry = self._entries.get(provider_id)
            if entry is None:
                return 0.0
            if not self._effective_routable(entry):
                return 0.0
            return self._weight_overrides.get(provider_id, entry.weight)

    def _effective_routable(self, entry: ProviderEntry) -> bool:
        """Routable under the *effective* flags (caller holds the lock)."""
        enabled = self._enabled_overrides.get(entry.id, entry.enabled)
        weight = self._weight_overrides.get(entry.id, entry.weight)
        return (
            enabled
            and weight > 0.0
            and entry.rollout is not RolloutState.DISABLED
        )

    def routable(self) -> list[ProviderEntry]:
        """Every entry eligible for routing under the effective flags/weights."""
        with self._lock:
            return [e for e in self._entries.values() if self._effective_routable(e)]

    # -- runtime registration --------------------------------------------- #

    def register(self, entry: ProviderEntry, *, replace: bool = False) -> None:
        """Add a runtime provider entry layered over the catalog.

        Args:
            entry: the provider to add.
            replace: when ``False`` (default), registering an existing id raises
                :class:`ValueError`; when ``True``, it overwrites in place.
        """
        with self._lock:
            if entry.id in self._entries and not replace:
                raise ValueError(
                    f"provider {entry.id!r} already registered (pass replace=True to overwrite)"
                )
            self._entries[entry.id] = entry
        logger.info("video_registry.registered", provider=entry.id, replace=replace)

    def unregister(self, provider_id: str) -> ProviderEntry | None:
        """Remove a provider (catalog or runtime) and any overrides for it.

        Returns the removed entry, or ``None`` if it was unknown.
        """
        with self._lock:
            entry = self._entries.pop(provider_id, None)
            self._enabled_overrides.pop(provider_id, None)
            self._weight_overrides.pop(provider_id, None)
        if entry is not None:
            logger.info("video_registry.unregistered", provider=provider_id)
        return entry

    # -- feature flags + weight overrides --------------------------------- #

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        """Force a provider on/off at runtime (an override over the catalog flag)."""
        with self._lock:
            if provider_id not in self._entries:
                raise KeyError(f"no such video provider: {provider_id!r}")
            self._enabled_overrides[provider_id] = enabled
        logger.info("video_registry.flag_set", provider=provider_id, enabled=enabled)

    def clear_enabled_override(self, provider_id: str) -> None:
        """Drop a runtime enable/disable override (revert to the catalog flag)."""
        with self._lock:
            self._enabled_overrides.pop(provider_id, None)

    def set_weight(self, provider_id: str, weight: float) -> None:
        """Override a provider's routing weight at runtime (live canary tuning)."""
        if weight < 0:
            raise ValueError(f"weight must be >= 0, got {weight}")
        with self._lock:
            if provider_id not in self._entries:
                raise KeyError(f"no such video provider: {provider_id!r}")
            self._weight_overrides[provider_id] = weight
        logger.info("video_registry.weight_set", provider=provider_id, weight=weight)

    def clear_weight_override(self, provider_id: str) -> None:
        """Drop a runtime weight override (revert to the catalog weight)."""
        with self._lock:
            self._weight_overrides.pop(provider_id, None)

    # -- capability queries ----------------------------------------------- #

    def query(
        self,
        *,
        mode: VideoMode | str | None = None,
        duration_s: float | None = None,
        resolution: Resolution | str | None = None,
        require_audio: bool = False,
        kind: ProviderKind | None = None,
        include_disabled: bool = False,
    ) -> list[ProviderEntry]:
        """Providers that can serve the (partial) request, best-weighted first.

        Filters by capability (mode/duration/resolution/audio) via
        :meth:`CapabilityProfile.satisfies`, optionally by :class:`ProviderKind`,
        and — unless ``include_disabled`` — to effectively-routable providers
        only. Results are sorted by effective weight descending (then id, for a
        stable tie-break), so the caller can take the head as the preferred pick.
        """
        with self._lock:
            entries = list(self._entries.values())
            results: list[ProviderEntry] = []
            for entry in entries:
                if not include_disabled and not self._effective_routable(entry):
                    continue
                if kind is not None and entry.kind is not kind:
                    continue
                if not entry.capabilities.satisfies(
                    mode=mode,
                    duration_s=duration_s,
                    resolution=resolution,
                    require_audio=require_audio,
                ):
                    continue
                results.append(entry)
            weight_of = {
                e.id: self._weight_overrides.get(e.id, e.weight) for e in results
            }
        results.sort(key=lambda e: (-weight_of[e.id], e.id))
        return results

    def candidates(
        self,
        *,
        mode: VideoMode | str | None = None,
        duration_s: float | None = None,
        resolution: Resolution | str | None = None,
        require_audio: bool = False,
        kind: ProviderKind | None = None,
    ) -> list[WeightedCandidate]:
        """Routable providers for the request as weighted picker candidates."""
        return [
            WeightedCandidate(id=e.id, weight=self.effective_weight(e.id))
            for e in self.query(
                mode=mode,
                duration_s=duration_s,
                resolution=resolution,
                require_audio=require_audio,
                kind=kind,
            )
        ]

    # -- weighted (canary / A-B) selection -------------------------------- #

    def pick(
        self,
        routing_key: str,
        *,
        mode: VideoMode | str | None = None,
        duration_s: float | None = None,
        resolution: Resolution | str | None = None,
        require_audio: bool = False,
        kind: ProviderKind | None = None,
        salt: str = DEFAULT_SALT,
    ) -> ProviderEntry | None:
        """Deterministically pick one routable provider for ``routing_key``.

        Filters to providers that can serve the request, then runs the weighted
        picker over their effective weights. Stable: the same key + candidate set
        always returns the same provider (so a retry stays put). ``None`` when no
        provider can serve the request.
        """
        candidates = self.candidates(
            mode=mode,
            duration_s=duration_s,
            resolution=resolution,
            require_audio=require_audio,
            kind=kind,
        )
        chosen_id = pick_weighted(candidates, routing_key, salt=salt)
        return self.get(chosen_id) if chosen_id is not None else None

    def expected_split(
        self,
        *,
        mode: VideoMode | str | None = None,
        duration_s: float | None = None,
        resolution: Resolution | str | None = None,
        require_audio: bool = False,
        kind: ProviderKind | None = None,
    ) -> dict[str, float]:
        """The ideal traffic share per provider for a request (weights ⇒ shares)."""
        return dict(
            expected_distribution(
                self.candidates(
                    mode=mode,
                    duration_s=duration_s,
                    resolution=resolution,
                    require_audio=require_audio,
                    kind=kind,
                )
            )
        )

    # -- hot reload ------------------------------------------------------- #

    def reload(
        self,
        *,
        text: str | None = None,
        path: str | Path | None = None,
        preserve_overrides: bool = True,
    ) -> ProviderCatalog:
        """Atomically swap the catalog from new text or a file.

        The new catalog is **parsed and validated first**; only on success is the
        live state swapped, so a malformed reload raises
        :class:`~app.video.registry.catalog.CatalogError` and leaves the running
        registry untouched. Exactly one of ``text`` / ``path`` must be given;
        with neither, the checked-in default catalog is reloaded.

        Runtime :meth:`register`-ed entries are dropped (the catalog is the new
        baseline). With ``preserve_overrides`` (default), flag/weight overrides
        for ids still present in the new catalog are kept; overrides for ids that
        vanished are discarded.
        """
        if text is not None and path is not None:
            raise ValueError("reload accepts at most one of text= / path=")
        if text is not None:
            new_catalog = load_catalog_text(text, source="<reload>")
        elif path is not None:
            new_catalog = load_catalog_file(path)
        else:
            new_catalog = load_default_catalog()

        with self._lock:
            old_overrides_enabled = self._enabled_overrides
            old_overrides_weight = self._weight_overrides
            self._catalog = new_catalog
            self._entries = {e.id: e for e in new_catalog.providers}
            if preserve_overrides:
                live_ids = set(self._entries)
                self._enabled_overrides = {
                    k: v for k, v in old_overrides_enabled.items() if k in live_ids
                }
                self._weight_overrides = {
                    k: v for k, v in old_overrides_weight.items() if k in live_ids
                }
            else:
                self._enabled_overrides = {}
                self._weight_overrides = {}
        logger.info(
            "video_registry.reloaded",
            providers=len(new_catalog.providers),
            preserve_overrides=preserve_overrides,
        )
        return new_catalog

    # -- snapshots -------------------------------------------------------- #

    def effective_profile(self, provider_id: str) -> CapabilityProfile | None:
        """The capability profile for a provider (``None`` if unknown)."""
        entry = self.get(provider_id)
        return entry.capabilities if entry is not None else None

    def snapshot(self) -> RegistrySnapshot:
        """A plain-data summary of the registry (for logs / introspection)."""
        with self._lock:
            return {
                "version": self._catalog.version,
                "total": len(self._entries),
                "routable": [
                    e.id for e in self._entries.values() if self._effective_routable(e)
                ],
                "enabled_overrides": dict(self._enabled_overrides),
                "weight_overrides": dict(self._weight_overrides),
            }


def register_runtime(
    registry: VideoProviderRegistry, entries: Iterable[ProviderEntry], *, replace: bool = False
) -> None:
    """Register several runtime entries at once (convenience for wiring/tests)."""
    for entry in entries:
        registry.register(entry, replace=replace)


__all__ = [
    "RegistrySnapshot",
    "VideoProviderRegistry",
    "register_runtime",
]
