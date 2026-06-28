"""Schema compatibility checking — detect breaking changes between two schemas.

A versioned public API needs a guard against *accidental* breaking changes. This
module diffs two assembled :class:`~app.graphql.schema.Schema` objects (e.g. the
deployed schema vs a candidate) and classifies each difference as **breaking**,
**dangerous**, or **safe**, mirroring the categories graphql-inspector uses:

* breaking — a removed type/field/enum-value, a field turned non-null→nullable on
  input, an argument added as required, a field's type changed incompatibly;
* dangerous — a new enum value (clients may not handle it), a new optional arg;
* safe — a new type/field/optional-arg, a new nullable field, a deprecation.

The result powers a CI gate (a candidate schema with breaking changes that are
not covered by the deprecation policy fails) and documents the contract's
evolution. It compares the *named-type registries*, so it works off the live
in-code schema, not a re-parsed SDL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.graphql.schema import Schema
from app.graphql.type_system import (
    GraphQLEnum,
    GraphQLInputObject,
    GraphQLInterface,
    GraphQLNonNull,
    GraphQLObject,
    type_ref_str,
)


class ChangeKind(StrEnum):
    BREAKING = "breaking"
    DANGEROUS = "dangerous"
    SAFE = "safe"


@dataclass(frozen=True, slots=True)
class SchemaChange:
    kind: ChangeKind
    message: str
    coordinate: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "message": self.message, "coordinate": self.coordinate}


def diff_schemas(old: Schema, new: Schema) -> list[SchemaChange]:
    """Return the classified changes from ``old`` → ``new``."""
    changes: list[SchemaChange] = []
    old_types = {t.name: t for t in old.named_types() if not t.name.startswith("__")}
    new_types = {t.name: t for t in new.named_types() if not t.name.startswith("__")}

    for name in old_types.keys() - new_types.keys():
        changes.append(SchemaChange(ChangeKind.BREAKING, f"Type {name!r} was removed.", name))
    for name in new_types.keys() - old_types.keys():
        changes.append(SchemaChange(ChangeKind.SAFE, f"Type {name!r} was added.", name))

    for name in old_types.keys() & new_types.keys():
        changes.extend(_diff_type(old_types[name], new_types[name]))
    return changes


def _diff_type(old: object, new: object) -> list[SchemaChange]:
    if isinstance(old, GraphQLObject) and isinstance(new, GraphQLObject):
        return _diff_fields(old.name, old.fields, new.fields, output=True)
    if isinstance(old, GraphQLInterface) and isinstance(new, GraphQLInterface):
        return _diff_fields(old.name, old.fields, new.fields, output=True)
    if isinstance(old, GraphQLInputObject) and isinstance(new, GraphQLInputObject):
        return _diff_input_fields(old.name, old.fields, new.fields)
    if isinstance(old, GraphQLEnum) and isinstance(new, GraphQLEnum):
        return _diff_enum(old, new)
    if type(old) is not type(new):
        return [
            SchemaChange(
                ChangeKind.BREAKING,
                f"Type {old.name!r} changed kind.",  # type: ignore[attr-defined]
                old.name,  # type: ignore[attr-defined]
            )
        ]
    return []


def _diff_fields(type_name: str, old: dict, new: dict, *, output: bool) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    for fname in old.keys() - new.keys():
        if fname.startswith("__"):
            continue
        changes.append(
            SchemaChange(
                ChangeKind.BREAKING,
                f"Field {type_name}.{fname} was removed.",
                f"{type_name}.{fname}",
            )
        )
    for fname in new.keys() - old.keys():
        if fname.startswith("__"):
            continue
        changes.append(
            SchemaChange(
                ChangeKind.SAFE,
                f"Field {type_name}.{fname} was added.",
                f"{type_name}.{fname}",
            )
        )
    for fname in old.keys() & new.keys():
        if fname.startswith("__"):
            continue
        coord = f"{type_name}.{fname}"
        old_t, new_t = type_ref_str(old[fname].type), type_ref_str(new[fname].type)
        if old_t != new_t:
            kind = ChangeKind.BREAKING if output else ChangeKind.DANGEROUS
            changes.append(
                SchemaChange(kind, f"{coord} changed type {old_t} → {new_t}.", coord)
            )
        changes.extend(_diff_args(coord, old[fname].args, new[fname].args))
    return changes


def _diff_args(coord: str, old: dict, new: dict) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    from app.graphql.type_system import UNDEFINED

    for aname in new.keys() - old.keys():
        arg = new[aname]
        required = isinstance(arg.type, GraphQLNonNull) and arg.default_value is UNDEFINED
        kind = ChangeKind.BREAKING if required else ChangeKind.DANGEROUS
        changes.append(
            SchemaChange(kind, f"Argument {coord}({aname}:) was added.", f"{coord}.{aname}")
        )
    for aname in old.keys() - new.keys():
        changes.append(
            SchemaChange(
                ChangeKind.BREAKING,
                f"Argument {coord}({aname}:) was removed.",
                f"{coord}.{aname}",
            )
        )
    return changes


def _diff_input_fields(type_name: str, old: dict, new: dict) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    from app.graphql.type_system import UNDEFINED

    for fname in new.keys() - old.keys():
        fdef = new[fname]
        required = isinstance(fdef.type, GraphQLNonNull) and fdef.default_value is UNDEFINED
        kind = ChangeKind.BREAKING if required else ChangeKind.SAFE
        changes.append(
            SchemaChange(
                kind, f"Input field {type_name}.{fname} was added.", f"{type_name}.{fname}"
            )
        )
    for fname in old.keys() - new.keys():
        changes.append(
            SchemaChange(
                ChangeKind.BREAKING,
                f"Input field {type_name}.{fname} was removed.",
                f"{type_name}.{fname}",
            )
        )
    return changes


def _diff_enum(old: GraphQLEnum, new: GraphQLEnum) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    old_v = {v.name for v in old.values}
    new_v = {v.name for v in new.values}
    for name in old_v - new_v:
        coord = f"{old.name}.{name}"
        changes.append(
            SchemaChange(ChangeKind.BREAKING, f"Enum value {coord} was removed.", coord)
        )
    for name in new_v - old_v:
        coord = f"{new.name}.{name}"
        changes.append(
            SchemaChange(ChangeKind.DANGEROUS, f"Enum value {coord} was added.", coord)
        )
    return changes


def breaking_changes(old: Schema, new: Schema) -> list[SchemaChange]:
    """Only the breaking changes (the CI gate's failing set)."""
    return [c for c in diff_schemas(old, new) if c.kind is ChangeKind.BREAKING]


def is_backward_compatible(old: Schema, new: Schema) -> bool:
    """True when ``new`` introduces no breaking changes over ``old``."""
    return not breaking_changes(old, new)


__all__ = [
    "ChangeKind",
    "SchemaChange",
    "breaking_changes",
    "diff_schemas",
    "is_backward_compatible",
]
