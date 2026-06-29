"""The feature-definition registry — entities, views, services, on-demand views.

The registry is the single source of truth for *what features exist*. It:

* registers entities, feature views, on-demand views, and feature services,
* assigns each feature view a **content-addressed version** from its fingerprint
  (so re-registering an identical definition is idempotent and any change mints a
  new version — the signal that drives re-materialisation),
* resolves :class:`FeatureRef` strings to concrete (view, feature) pairs at a
  pinned-or-latest version,
* validates references (an unknown view/feature raises early), and
* holds the on-demand transform callables (kept off the value objects so those
  stay pure/serialisable).

It is in-memory and deterministic. Durable persistence of definitions is a
separate concern (a snapshot can be written via :mod:`store` / the DB layer);
the registry itself never does I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence

from .rows import EntityRow
from .types import (
    DefinitionError,
    Entity,
    FeatureRef,
    FeatureService,
    FeatureSpec,
    FeatureView,
    OnDemandFeatureView,
    ReferenceError,
    parse_refs,
)

#: An on-demand transform: (request_inputs, upstream_feature_values) -> outputs.
OnDemandFn = Callable[[Mapping[str, object], Mapping[str, object]], Mapping[str, object]]


def _version_from_fingerprint(fingerprint: tuple[object, ...]) -> int:
    """Stable positive integer version from a definition fingerprint.

    A 31-bit hash of the canonical-JSON-encoded fingerprint. Collisions are
    astronomically unlikely for the cardinalities a feature registry sees, and the
    property we need — *identical definition → identical version, any change →
    different version* — holds by construction.
    """
    blob = json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(blob).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class FeatureRegistry:
    """In-memory registry of feature definitions with content-addressed versions."""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        # view name -> {version -> FeatureView}; plus the latest version per name.
        self._views: dict[str, dict[int, FeatureView]] = {}
        self._latest: dict[str, int] = {}
        self._services: dict[str, FeatureService] = {}
        self._on_demand: dict[str, OnDemandFeatureView] = {}
        self._on_demand_fns: dict[str, OnDemandFn] = {}

    # -- entities -------------------------------------------------------- #

    def register_entity(self, entity: Entity) -> Entity:
        existing = self._entities.get(entity.name)
        if existing is not None and existing.fingerprint() != entity.fingerprint():
            raise DefinitionError(
                f"entity {entity.name!r} already registered with a different definition"
            )
        self._entities[entity.name] = entity
        return entity

    def get_entity(self, name: str) -> Entity:
        try:
            return self._entities[name]
        except KeyError as exc:
            raise ReferenceError(f"unknown entity {name!r}") from exc

    def list_entities(self) -> list[Entity]:
        return [self._entities[k] for k in sorted(self._entities)]

    # -- feature views --------------------------------------------------- #

    def register_feature_view(self, view: FeatureView) -> FeatureView:
        """Register a view; stamp + return it with its content-addressed version.

        Entities referenced by the view are auto-registered if compatible. Re-
        registering a structurally identical view is idempotent (same version).
        """
        for ent in view.entities:
            self.register_entity(ent)
        version = _version_from_fingerprint(view.fingerprint())
        stamped = view.with_version(version)
        versions = self._views.setdefault(view.name, {})
        if version not in versions:
            versions[version] = stamped
        # "Latest" tracks the most recently registered version of a name.
        self._latest[view.name] = version
        return stamped

    def get_feature_view(self, name: str, *, version: int | None = None) -> FeatureView:
        versions = self._views.get(name)
        if not versions:
            raise ReferenceError(f"unknown feature view {name!r}")
        if version is None:
            return versions[self._latest[name]]
        try:
            return versions[version]
        except KeyError as exc:
            raise ReferenceError(
                f"feature view {name!r} has no version {version} "
                f"(known: {sorted(versions)})"
            ) from exc

    def feature_view_versions(self, name: str) -> list[int]:
        return sorted(self._views.get(name, {}))

    def list_feature_views(self) -> list[FeatureView]:
        """The latest version of every registered feature view (name-sorted)."""
        return [self._views[name][self._latest[name]] for name in sorted(self._views)]

    # -- on-demand views ------------------------------------------------- #

    def register_on_demand_view(
        self, view: OnDemandFeatureView, fn: OnDemandFn
    ) -> OnDemandFeatureView:
        if view.name in self._on_demand:
            existing = self._on_demand[view.name]
            if existing != view:
                raise DefinitionError(
                    f"on-demand view {view.name!r} already registered differently"
                )
        for src in view.source_views:
            if src not in self._views:
                raise ReferenceError(
                    f"on-demand view {view.name!r} sources unknown view {src!r}"
                )
        self._on_demand[view.name] = view
        self._on_demand_fns[view.name] = fn
        return view

    def get_on_demand_view(self, name: str) -> OnDemandFeatureView:
        try:
            return self._on_demand[name]
        except KeyError as exc:
            raise ReferenceError(f"unknown on-demand view {name!r}") from exc

    def on_demand_fn(self, name: str) -> OnDemandFn:
        return self._on_demand_fns[name]

    def list_on_demand_views(self) -> list[OnDemandFeatureView]:
        return [self._on_demand[k] for k in sorted(self._on_demand)]

    # -- feature services ------------------------------------------------ #

    def register_feature_service(self, service: FeatureService) -> FeatureService:
        # Validate every reference resolves before accepting the service.
        for ref in service.refs():
            self.resolve(ref)
        existing = self._services.get(service.name)
        if existing is not None and existing != service:
            raise DefinitionError(
                f"feature service {service.name!r} already registered differently"
            )
        self._services[service.name] = service
        return service

    def get_feature_service(self, name: str) -> FeatureService:
        try:
            return self._services[name]
        except KeyError as exc:
            raise ReferenceError(f"unknown feature service {name!r}") from exc

    def list_feature_services(self) -> list[FeatureService]:
        return [self._services[k] for k in sorted(self._services)]

    # -- reference resolution ------------------------------------------- #

    def resolve(self, ref: FeatureRef) -> tuple[FeatureView, FeatureSpec]:
        """Resolve a reference to its (view, feature_spec); raises if unknown."""
        view = self.get_feature_view(ref.view, version=ref.version)
        spec = view.feature(ref.feature)  # raises ReferenceError if missing
        return view, spec

    def resolve_service(self, name: str) -> list[tuple[FeatureView, FeatureSpec]]:
        service = self.get_feature_service(name)
        return [self.resolve(ref) for ref in service.refs()]

    def views_for_refs(
        self, refs: Sequence[str | FeatureRef]
    ) -> tuple[list[FeatureView], list[FeatureRef]]:
        """Deduplicate the views needed for a set of refs (preserving ref order).

        Returns ``(unique_views, parsed_refs)`` — the views to run the
        point-in-time join over and the parsed references in request order.
        """
        parsed = parse_refs(refs)
        seen: dict[tuple[str, int], FeatureView] = {}
        for ref in parsed:
            view, _ = self.resolve(ref)
            seen.setdefault((view.name, view.version), view)
        return list(seen.values()), list(parsed)

    # -- on-demand evaluation (the request-time computation seam) -------- #

    def evaluate_on_demand(
        self, name: str, *, request: Mapping[str, object], upstream: Mapping[str, object]
    ) -> dict[str, object]:
        """Run an on-demand transform and validate it emits the declared features."""
        view = self.get_on_demand_view(name)
        fn = self._on_demand_fns[name]
        out = dict(fn(request, upstream))
        missing = [f.name for f in view.features if f.name not in out]
        if missing:
            raise DefinitionError(
                f"on-demand view {name!r} transform did not emit features {missing}"
            )
        return {f.name: out[f.name] for f in view.features}


def request_inputs_for(entity: EntityRow, names: Sequence[str]) -> dict[str, object]:
    """Pull the named request inputs off an entity row (missing → ``None``)."""
    return {n: entity.request.get(n) for n in names}


__all__ = ["FeatureRegistry", "OnDemandFn", "request_inputs_for"]
