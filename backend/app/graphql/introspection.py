"""Introspection support: the ``__schema``/``__type`` roots + an SDL printer.

Two faces of the same type registry:

* :func:`introspection_query_fields` adds the standard ``__schema`` and
  ``__type`` fields to the Query root, backed by a code-built introspection
  object graph (``__Schema``/``__Type``/``__Field``/``__InputValue``/
  ``__EnumValue``/``__Directive``) so a GraphQL client (or codegen tool) can
  discover the schema at runtime;
* :func:`print_schema` emits the schema as a stable SDL string for export
  (``GET /graphql/schema``) and for documentation/diffing.

Both read the same :class:`~app.graphql.schema.Schema`, so the runtime
introspection and the printed SDL never drift.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.graphql.scalars import GraphQLBoolean, GraphQLString
from app.graphql.schema import Schema
from app.graphql.type_system import (
    UNDEFINED,
    Argument,
    Field,
    GraphQLEnum,
    GraphQLInputObject,
    GraphQLInterface,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
    GraphQLType,
    GraphQLUnion,
    type_ref_str,
)
from app.graphql.versioning import deprecation_reason

# --------------------------------------------------------------------------- #
# Runtime introspection (the __schema / __type object graph)
# --------------------------------------------------------------------------- #

# Kind enum values per the spec.
_KIND = {
    "SCALAR": "SCALAR",
    "OBJECT": "OBJECT",
    "INTERFACE": "INTERFACE",
    "UNION": "UNION",
    "ENUM": "ENUM",
    "INPUT_OBJECT": "INPUT_OBJECT",
    "LIST": "LIST",
    "NON_NULL": "NON_NULL",
}


def _type_ref(t: GraphQLType) -> dict[str, Any]:
    """A JSON ``__Type`` reference (recursively encodes wrappers)."""
    if isinstance(t, GraphQLNonNull):
        return {"kind": "NON_NULL", "name": None, "ofType": _type_ref(t.of_type)}
    if isinstance(t, GraphQLList):
        return {"kind": "LIST", "name": None, "ofType": _type_ref(t.of_type)}
    assert isinstance(t, GraphQLNamedType)
    return {"kind": _kind_of(t), "name": t.name, "ofType": None, "__named": t}


def _kind_of(t: GraphQLNamedType) -> str:
    if isinstance(t, GraphQLScalar):
        return "SCALAR"
    if isinstance(t, GraphQLObject):
        return "OBJECT"
    if isinstance(t, GraphQLInterface):
        return "INTERFACE"
    if isinstance(t, GraphQLUnion):
        return "UNION"
    if isinstance(t, GraphQLEnum):
        return "ENUM"
    if isinstance(t, GraphQLInputObject):
        return "INPUT_OBJECT"
    return "SCALAR"  # pragma: no cover


def _full_type(schema: Schema, t: GraphQLNamedType) -> dict[str, Any]:
    """A fully-described ``__Type`` for a named type."""
    out: dict[str, Any] = {
        "kind": _kind_of(t),
        "name": t.name,
        "description": getattr(t, "description", None),
        "fields": None,
        "inputFields": None,
        "interfaces": None,
        "enumValues": None,
        "possibleTypes": None,
        "ofType": None,
    }
    if isinstance(t, (GraphQLObject, GraphQLInterface)):
        out["fields"] = [
            _field_intro(schema, name, f, f"{t.name}.{name}")
            for name, f in t.fields.items()
            if not name.startswith("__")
        ]
        if isinstance(t, GraphQLObject):
            out["interfaces"] = [_type_ref(i) for i in t.interfaces]
        else:
            out["interfaces"] = []
            out["possibleTypes"] = [
                _type_ref(o) for o in schema.implementations.get(t.name, [])
            ]
    elif isinstance(t, GraphQLUnion):
        out["possibleTypes"] = [_type_ref(m) for m in t.types]
    elif isinstance(t, GraphQLEnum):
        out["enumValues"] = [
            {
                "name": v.name,
                "description": v.description,
                "isDeprecated": v.deprecation_reason is not None,
                "deprecationReason": v.deprecation_reason,
            }
            for v in t.values
        ]
    elif isinstance(t, GraphQLInputObject):
        out["inputFields"] = [
            _input_value(name, f.type, f.default_value, f.description)
            for name, f in t.fields.items()
        ]
    return out


def _field_intro(schema: Schema, name: str, f: Field, coordinate: str) -> dict[str, Any]:
    reason = f.deprecation_reason or deprecation_reason(coordinate)
    return {
        "name": name,
        "description": f.description,
        "args": [
            _input_value(arg_name, arg.type, arg.default_value, arg.description)
            for arg_name, arg in f.args.items()
        ],
        "type": _type_ref(f.type),
        "isDeprecated": reason is not None,
        "deprecationReason": reason,
    }


def _input_value(
    name: str, t: GraphQLType, default: Any, description: str | None
) -> dict[str, Any]:
    default_str: str | None = None
    if default is not UNDEFINED and default is not None:
        default_str = _format_default(default)
    return {
        "name": name,
        "description": description,
        "type": _type_ref(t),
        "defaultValue": default_str,
    }


def _format_default(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_default(v) for v in value) + "]"
    return str(value)


def build_introspection(schema: Schema) -> dict[str, Any]:
    """The value the ``__schema`` field resolves to (a JSON ``__Schema``)."""
    types = [_full_type(schema, t) for t in schema.named_types()]
    return {
        "description": "Kinora public GraphQL API.",
        "queryType": {"name": schema.query.name},
        "mutationType": {"name": schema.mutation.name} if schema.mutation else None,
        "subscriptionType": (
            {"name": schema.subscription.name} if schema.subscription else None
        ),
        "types": types,
        "directives": [
            {
                "name": "skip",
                "description": "Skip this field when the `if` argument is true.",
                "locations": ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
                "args": [_input_value("if", GraphQLNonNull(GraphQLBoolean), UNDEFINED, None)],
            },
            {
                "name": "include",
                "description": "Include this field only when the `if` argument is true.",
                "locations": ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
                "args": [_input_value("if", GraphQLNonNull(GraphQLBoolean), UNDEFINED, None)],
            },
            {
                "name": "deprecated",
                "description": "Marks an element as deprecated with an optional reason.",
                "locations": ["FIELD_DEFINITION", "ENUM_VALUE", "ARGUMENT_DEFINITION"],
                "args": [_input_value("reason", GraphQLString, "No longer supported", None)],
            },
        ],
    }


def introspection_query_fields(
    schema_ref: Schema | Callable[[], Schema],
) -> dict[str, Field]:
    """The ``__schema`` and ``__type`` Query fields, bound to a schema.

    ``schema_ref`` may be a :class:`Schema` or a zero-arg callable returning the
    schema; the callable form lets the roots be assembled before the final schema
    object exists (the resolver reads the live schema at request time).
    """
    from app.graphql.root_types import IntrospectionTypes

    intro = IntrospectionTypes.get()

    def _schema() -> Schema:
        return schema_ref() if callable(schema_ref) else schema_ref

    def resolve_schema(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> Any:
        return build_introspection(_schema())

    def resolve_type(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> Any:
        schema = _schema()
        named = schema.get_type(args["name"])
        return _full_type(schema, named) if named is not None else None

    return {
        "__schema": Field(
            type=GraphQLNonNull(intro["__Schema"]),
            resolver=resolve_schema,
            description="Access the current type schema of this server.",
            cost=0,
        ),
        "__type": Field(
            type=intro["__Type"],
            args={"name": Argument(GraphQLNonNull(GraphQLString))},
            resolver=resolve_type,
            description="Request the type information of a single named type.",
            cost=0,
        ),
    }


# --------------------------------------------------------------------------- #
# SDL printer
# --------------------------------------------------------------------------- #


def print_schema(schema: Schema) -> str:
    """Print the schema as a stable SDL document (for export + diffing)."""
    blocks: list[str] = [_print_schema_def(schema)]
    for t in schema.named_types():
        if t.name.startswith("__"):
            continue
        block = _print_type(schema, t)
        if block:
            blocks.append(block)
    return "\n\n".join(b for b in blocks if b) + "\n"


def _print_schema_def(schema: Schema) -> str:
    lines = ["schema {", f"  query: {schema.query.name}"]
    if schema.mutation:
        lines.append(f"  mutation: {schema.mutation.name}")
    if schema.subscription:
        lines.append(f"  subscription: {schema.subscription.name}")
    lines.append("}")
    return "\n".join(lines)


def _print_type(schema: Schema, t: GraphQLNamedType) -> str:
    if isinstance(t, GraphQLScalar):
        if t.name in {"Int", "Float", "String", "Boolean", "ID"}:
            return ""  # built-ins are implicit in SDL
        return _doc(t.description) + f"scalar {t.name}"
    if isinstance(t, GraphQLEnum):
        body = "\n".join(f"  {v.name}{_dep(v.deprecation_reason)}" for v in t.values)
        return _doc(t.description) + f"enum {t.name} {{\n{body}\n}}"
    if isinstance(t, GraphQLObject):
        impl = ""
        if t.interfaces:
            impl = " implements " + " & ".join(i.name for i in t.interfaces)
        body = _print_fields(t.fields, t.name)
        return _doc(t.description) + f"type {t.name}{impl} {{\n{body}\n}}"
    if isinstance(t, GraphQLInterface):
        body = _print_fields(t.fields, t.name)
        return _doc(t.description) + f"interface {t.name} {{\n{body}\n}}"
    if isinstance(t, GraphQLUnion):
        members = " | ".join(m.name for m in t.types)
        return _doc(t.description) + f"union {t.name} = {members}"
    if isinstance(t, GraphQLInputObject):
        body = "\n".join(
            f"  {name}: {type_ref_str(f.type)}{_default(f.default_value)}"
            for name, f in t.fields.items()
        )
        return _doc(t.description) + f"input {t.name} {{\n{body}\n}}"
    return ""  # pragma: no cover


def _print_fields(fields: dict[str, Field], type_name: str) -> str:
    lines: list[str] = []
    for name, f in fields.items():
        if name.startswith("__"):
            continue
        args = ""
        if f.args:
            args = "(" + ", ".join(
                f"{a}: {type_ref_str(arg.type)}{_default(arg.default_value)}"
                for a, arg in f.args.items()
            ) + ")"
        reason = f.deprecation_reason or deprecation_reason(f"{type_name}.{name}")
        lines.append(f"  {name}{args}: {type_ref_str(f.type)}{_dep(reason)}")
    return "\n".join(lines)


def _default(value: Any) -> str:
    if value is UNDEFINED or value is None:
        return ""
    return f" = {_format_default(value)}"


def _dep(reason: str | None) -> str:
    if not reason:
        return ""
    return f' @deprecated(reason: "{reason}")'


def _doc(description: str | None) -> str:
    if not description:
        return ""
    return f'"""{description}"""\n'


__all__ = [
    "build_introspection",
    "introspection_query_fields",
    "print_schema",
]
