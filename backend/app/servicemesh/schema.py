"""Structural message schemas + deterministic content hashing.

A :class:`MessageSchema` is a *structural* description of a message body: an
ordered set of named fields, each with a coarse type, a required/optional flag, a
nullability flag, and (for enums/numbers) a value domain. We deliberately describe
structure rather than carry a full JSON-Schema document because the compatibility
checker (:mod:`app.servicemesh.compatibility`) reasons field-by-field, and a
compact canonical form gives a *stable content hash* that survives field
reordering and cosmetic edits.

Two ways to obtain a schema:

* :meth:`MessageSchema.from_fields` — declare fields by hand (used by tests and by
  hand-rolled queue/pubsub contracts).
* :meth:`MessageSchema.from_model` — derive one from a pydantic v2 ``BaseModel`` so
  an existing DTO can be registered without re-typing its shape.

The content hash is a SHA-256 over a canonical JSON projection with sorted keys, so
the same logical schema always hashes identically regardless of declaration order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from app.servicemesh.versioning import SemVer

__all__ = ["FieldType", "FieldSpec", "MessageSchema"]


class FieldType(StrEnum):
    """The coarse type lattice the compatibility checker reasons over.

    Coarse on purpose: the checker treats a widen (``INTEGER`` -> ``NUMBER``) as
    compatible and a narrow (``NUMBER`` -> ``INTEGER``) as breaking, so we only
    need the handful of buckets that capture those relations.
    """

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"
    ENUM = "enum"
    ANY = "any"


# A widening lattice: a value of type ``key`` can always be read as any type in the
# set. Used to classify a type change as compatible (widen) vs breaking (narrow).
_WIDENS_TO: dict[FieldType, frozenset[FieldType]] = {
    FieldType.INTEGER: frozenset({FieldType.INTEGER, FieldType.NUMBER, FieldType.ANY}),
    FieldType.NUMBER: frozenset({FieldType.NUMBER, FieldType.ANY}),
    FieldType.STRING: frozenset({FieldType.STRING, FieldType.ANY}),
    FieldType.BOOLEAN: frozenset({FieldType.BOOLEAN, FieldType.ANY}),
    FieldType.OBJECT: frozenset({FieldType.OBJECT, FieldType.ANY}),
    FieldType.ARRAY: frozenset({FieldType.ARRAY, FieldType.ANY}),
    FieldType.ENUM: frozenset({FieldType.ENUM, FieldType.STRING, FieldType.ANY}),
    FieldType.ANY: frozenset({FieldType.ANY}),
}


def widens_to(src: FieldType, dst: FieldType) -> bool:
    """True when a ``src`` value can be safely read as ``dst`` (no narrowing)."""
    return dst in _WIDENS_TO[src]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field in a message schema."""

    name: str
    type: FieldType
    required: bool = True
    nullable: bool = False
    # For ENUM fields, the permitted symbol set (a closed domain). Empty otherwise.
    enum_values: frozenset[str] = field(default_factory=frozenset)
    # For ARRAY fields, the element type (coarse). ANY when unknown.
    item_type: FieldType = FieldType.ANY
    description: str = ""

    def canonical(self) -> dict[str, Any]:
        """A stable, hash-friendly projection (sorted enum values, no docs)."""
        return {
            "name": self.name,
            "type": self.type.value,
            "required": self.required,
            "nullable": self.nullable,
            "enum_values": sorted(self.enum_values),
            "item_type": self.item_type.value,
        }


@dataclass(frozen=True, slots=True)
class MessageSchema:
    """A versioned, structurally-described message body."""

    schema_id: str
    version: SemVer
    fields: tuple[FieldSpec, ...]
    title: str = ""

    # -- derived ------------------------------------------------------------ #
    @property
    def fields_by_name(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    def canonical(self) -> dict[str, Any]:
        """Canonical form used for hashing — fields sorted by name."""
        return {
            "schema_id": self.schema_id,
            "version": str(self.version),
            "fields": [f.canonical() for f in sorted(self.fields, key=lambda f: f.name)],
        }

    def content_hash(self) -> str:
        """A deterministic ``sha256:<hex>`` digest of the canonical form.

        Stable under field reordering and independent of titles/descriptions, so
        the registry can detect "same shape, re-registered" vs a genuine change.
        """
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def key(self) -> tuple[str, SemVer]:
        """The registry key: ``(schema_id, version)``."""
        return (self.schema_id, self.version)

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_fields(
        cls,
        schema_id: str,
        version: SemVer | str,
        fields: list[FieldSpec],
        *,
        title: str = "",
    ) -> MessageSchema:
        """Build a schema from an explicit field list."""
        return cls(
            schema_id=schema_id,
            version=SemVer.coerce(version),
            fields=tuple(fields),
            title=title,
        )

    @classmethod
    def from_model(
        cls,
        schema_id: str,
        version: SemVer | str,
        model: type[BaseModel],
    ) -> MessageSchema:
        """Derive a schema from a pydantic v2 model's field annotations.

        Best-effort and coarse: it maps Python annotations onto the
        :class:`FieldType` lattice. Unknown types fall back to ``ANY`` so the
        schema is always constructible; the compatibility checker stays sound
        because ``ANY`` is the top of the widening lattice.
        """
        specs: list[FieldSpec] = []
        for name, info in model.model_fields.items():
            annotation = info.annotation
            ftype, nullable, item_type, enum_values = _classify_annotation(annotation)
            specs.append(
                FieldSpec(
                    name=name,
                    type=ftype,
                    required=info.is_required(),
                    nullable=nullable,
                    enum_values=enum_values,
                    item_type=item_type,
                )
            )
        return cls.from_fields(schema_id, version, specs, title=model.__name__)


def _classify_annotation(
    annotation: Any,
) -> tuple[FieldType, bool, FieldType, frozenset[str]]:
    """Map a Python annotation onto ``(type, nullable, item_type, enum_values)``."""
    nullable = False
    origin = get_origin(annotation)

    # Unwrap Optional / X | None.
    if origin is not None and _is_union(origin, annotation):
        args = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(args) != len(get_args(annotation))
        if len(args) == 1:
            inner, _n, item_type, enum_values = _classify_annotation(args[0])
            return inner, nullable or _n, item_type, enum_values
        return FieldType.ANY, nullable, FieldType.ANY, frozenset()

    if annotation in (None, type(None)):
        return FieldType.ANY, True, FieldType.ANY, frozenset()

    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return (
            FieldType.ENUM,
            nullable,
            FieldType.ANY,
            frozenset(str(m.value) for m in annotation),
        )

    if origin in (list, tuple, set, frozenset):
        elem_args = get_args(annotation)
        item_type = _classify_annotation(elem_args[0])[0] if elem_args else FieldType.ANY
        return FieldType.ARRAY, nullable, item_type, frozenset()

    if origin in (dict,):
        return FieldType.OBJECT, nullable, FieldType.ANY, frozenset()

    if isinstance(annotation, type):
        if issubclass(annotation, bool):
            return FieldType.BOOLEAN, nullable, FieldType.ANY, frozenset()
        if issubclass(annotation, int):
            return FieldType.INTEGER, nullable, FieldType.ANY, frozenset()
        if issubclass(annotation, float):
            return FieldType.NUMBER, nullable, FieldType.ANY, frozenset()
        if issubclass(annotation, str):
            return FieldType.STRING, nullable, FieldType.ANY, frozenset()
        if issubclass(annotation, BaseModel) or issubclass(annotation, dict):
            return FieldType.OBJECT, nullable, FieldType.ANY, frozenset()

    return FieldType.ANY, nullable, FieldType.ANY, frozenset()


def _is_union(origin: Any, annotation: Any) -> bool:
    """Detect both ``typing.Union`` and the PEP 604 ``X | Y`` origins."""
    import types
    import typing

    return origin is typing.Union or origin is types.UnionType
