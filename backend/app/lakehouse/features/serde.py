"""(De)serialisation of feature definitions ↔ JSON (for the durable registry).

The registry holds frozen value objects; the durable ``feature_store_view_defs``
table holds JSON. These pure functions bridge the two so a registry can be
snapshotted to the DB and rehydrated on startup, and so a feature-view version's
definition is auditable. The round-trip is exact (``loads(dumps(view)) == view``
once the content-addressed version is re-stamped by the registry), which the
serde test asserts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from .types import (
    Entity,
    FeatureSource,
    FeatureSpec,
    FeatureView,
    Transformation,
    ValueType,
)


def entity_to_dict(entity: Entity) -> dict[str, object]:
    return {
        "name": entity.name,
        "join_key": entity.join_key,
        "value_type": entity.value_type.value,
        "description": entity.description,
    }


def entity_from_dict(data: Mapping[str, Any]) -> Entity:
    return Entity(
        name=str(data["name"]),
        join_key=str(data.get("join_key", "")),
        value_type=ValueType(str(data.get("value_type", ValueType.STRING.value))),
        description=str(data.get("description", "")),
    )


def spec_to_dict(spec: FeatureSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "dtype": spec.dtype.value,
        "default": spec.default,
        "description": spec.description,
    }


def spec_from_dict(data: Mapping[str, Any]) -> FeatureSpec:
    return FeatureSpec(
        name=str(data["name"]),
        dtype=ValueType(str(data["dtype"])),
        default=data.get("default"),
        description=str(data.get("description", "")),
    )


def source_to_dict(source: FeatureSource) -> dict[str, object]:
    return {
        "name": source.name,
        "timestamp_field": source.timestamp_field,
        "created_field": source.created_field,
        "kind": source.kind,
    }


def source_from_dict(data: Mapping[str, Any]) -> FeatureSource:
    created = data.get("created_field")
    return FeatureSource(
        name=str(data["name"]),
        timestamp_field=str(data.get("timestamp_field", "event_timestamp")),
        created_field=None if created is None else str(created),
        kind=str(data.get("kind", "batch")),
    )


def transformation_to_dict(t: Transformation) -> dict[str, object]:
    return {
        "name": t.name,
        "expression": t.expression,
        "inputs": list(t.inputs),
        "mode": t.mode,
    }


def transformation_from_dict(data: Mapping[str, Any]) -> Transformation:
    inputs = data.get("inputs") or []
    return Transformation(
        name=str(data["name"]),
        expression=str(data.get("expression", "")),
        inputs=tuple(str(i) for i in inputs),
        mode=str(data.get("mode", "batch")),
    )


def feature_view_to_dict(view: FeatureView) -> dict[str, object]:
    """Serialise a feature view to a JSON-ready dict (the durable definition)."""
    return {
        "name": view.name,
        "entities": [entity_to_dict(e) for e in view.entities],
        "features": [spec_to_dict(f) for f in view.features],
        "source": source_to_dict(view.source),
        "ttl_seconds": None if view.ttl is None else view.ttl.total_seconds(),
        "transformation": (
            None if view.transformation is None else transformation_to_dict(view.transformation)
        ),
        "online": view.online,
        "tags": dict(view.tags),
        "description": view.description,
        "version": view.version,
        "owner": view.owner,
    }


def feature_view_from_dict(data: Mapping[str, Any]) -> FeatureView:
    """Rehydrate a feature view from its durable definition (version preserved)."""
    ttl_s = data.get("ttl_seconds")
    transform = data.get("transformation")
    tags = data.get("tags") or {}
    return FeatureView(
        name=str(data["name"]),
        entities=tuple(entity_from_dict(e) for e in data["entities"]),
        features=tuple(spec_from_dict(f) for f in data["features"]),
        source=source_from_dict(data["source"]),
        ttl=None if ttl_s is None else timedelta(seconds=float(ttl_s)),
        transformation=(
            None if transform is None else transformation_from_dict(transform)
        ),
        online=bool(data.get("online", True)),
        tags={str(k): str(v) for k, v in tags.items()},
        description=str(data.get("description", "")),
        version=int(data.get("version", 0)),
        owner=str(data.get("owner", "")),
    )


__all__ = [
    "entity_from_dict",
    "entity_to_dict",
    "feature_view_from_dict",
    "feature_view_to_dict",
    "source_from_dict",
    "source_to_dict",
    "spec_from_dict",
    "spec_to_dict",
    "transformation_from_dict",
    "transformation_to_dict",
]
