"""The introspection object types (``__Schema``, ``__Type``, …) built in code.

These mirror the GraphQL spec's introspection schema. They are plain
:class:`~app.graphql.type_system.GraphQLObject` types whose resolvers read the
JSON ``__Type`` dictionaries produced by ``app/graphql/introspection.py`` — the
default attribute/key resolver works for most fields, with a couple of custom
resolvers where the JSON key differs from the field name or needs filtering.

They are cached as a process-global singleton so the same instances are reused
across every assembled schema (a schema collects them via ``extra_types`` only
when introspection is wired into the Query root).
"""

from __future__ import annotations

from typing import Any

from app.graphql.scalars import GraphQLBoolean, GraphQLString
from app.graphql.type_system import (
    Argument,
    Field,
    GraphQLEnum,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObject,
)

_TYPE_KIND = GraphQLEnum(
    "__TypeKind",
    [
        "SCALAR",
        "OBJECT",
        "INTERFACE",
        "UNION",
        "ENUM",
        "INPUT_OBJECT",
        "LIST",
        "NON_NULL",
    ],
    description="An enum describing what kind of type a given `__Type` is.",
)

_DIRECTIVE_LOCATION = GraphQLEnum(
    "__DirectiveLocation",
    [
        "QUERY",
        "MUTATION",
        "SUBSCRIPTION",
        "FIELD",
        "FRAGMENT_DEFINITION",
        "FRAGMENT_SPREAD",
        "INLINE_FRAGMENT",
        "FIELD_DEFINITION",
        "ENUM_VALUE",
        "ARGUMENT_DEFINITION",
        "INPUT_FIELD_DEFINITION",
    ],
    description="A location a directive may be placed.",
)


def _build() -> dict[str, GraphQLNamedType]:
    # Forward declarations via thunks so the cyclic refs (Type<->Field) resolve.
    type_obj_ref: dict[str, GraphQLObject] = {}

    input_value = GraphQLObject(
        "__InputValue",
        lambda: {
            "name": Field(GraphQLNonNull(GraphQLString)),
            "description": Field(GraphQLString),
            "type": Field(GraphQLNonNull(type_obj_ref["__Type"])),
            "defaultValue": Field(GraphQLString),
        },
        description="An input value of an argument or input-object field.",
    )

    field_obj = GraphQLObject(
        "__Field",
        lambda: {
            "name": Field(GraphQLNonNull(GraphQLString)),
            "description": Field(GraphQLString),
            "args": Field(GraphQLNonNull(GraphQLList(GraphQLNonNull(input_value)))),
            "type": Field(GraphQLNonNull(type_obj_ref["__Type"])),
            "isDeprecated": Field(GraphQLNonNull(GraphQLBoolean)),
            "deprecationReason": Field(GraphQLString),
        },
        description="A field of an object or interface type.",
    )

    enum_value = GraphQLObject(
        "__EnumValue",
        lambda: {
            "name": Field(GraphQLNonNull(GraphQLString)),
            "description": Field(GraphQLString),
            "isDeprecated": Field(GraphQLNonNull(GraphQLBoolean)),
            "deprecationReason": Field(GraphQLString),
        },
        description="One value within an enum type.",
    )

    def _resolve_fields(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> Any:
        fields = source.get("fields")
        if fields is None:
            return None
        if args.get("includeDeprecated"):
            return fields
        return [f for f in fields if not f.get("isDeprecated")]

    type_obj = GraphQLObject(
        "__Type",
        lambda: {
            "kind": Field(GraphQLNonNull(_TYPE_KIND)),
            "name": Field(GraphQLString),
            "description": Field(GraphQLString),
            "fields": Field(
                GraphQLList(GraphQLNonNull(field_obj)),
                args={
                    "includeDeprecated": Argument(GraphQLBoolean, default_value=False)
                },
                resolver=_resolve_fields,
            ),
            "interfaces": Field(GraphQLList(GraphQLNonNull(type_obj_ref["__Type"]))),
            "possibleTypes": Field(GraphQLList(GraphQLNonNull(type_obj_ref["__Type"]))),
            "enumValues": Field(
                GraphQLList(GraphQLNonNull(enum_value)),
                args={
                    "includeDeprecated": Argument(GraphQLBoolean, default_value=False)
                },
                resolver=_resolve_enum_values,
            ),
            "inputFields": Field(GraphQLList(GraphQLNonNull(input_value))),
            "ofType": Field(type_obj_ref["__Type"]),
        },
        description="A type in the GraphQL schema (object, scalar, wrapper, …).",
    )
    type_obj_ref["__Type"] = type_obj

    directive_obj = GraphQLObject(
        "__Directive",
        lambda: {
            "name": Field(GraphQLNonNull(GraphQLString)),
            "description": Field(GraphQLString),
            "locations": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(_DIRECTIVE_LOCATION)))
            ),
            "args": Field(GraphQLNonNull(GraphQLList(GraphQLNonNull(input_value)))),
        },
        description="A directive supported by the server.",
    )

    schema_obj = GraphQLObject(
        "__Schema",
        lambda: {
            "description": Field(GraphQLString),
            "types": Field(GraphQLNonNull(GraphQLList(GraphQLNonNull(type_obj)))),
            "queryType": Field(GraphQLNonNull(type_obj)),
            "mutationType": Field(type_obj),
            "subscriptionType": Field(type_obj),
            "directives": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(directive_obj)))
            ),
        },
        description="The full schema of the server: its types and directives.",
    )

    return {
        "__Schema": schema_obj,
        "__Type": type_obj,
        "__Field": field_obj,
        "__InputValue": input_value,
        "__EnumValue": enum_value,
        "__Directive": directive_obj,
        "__TypeKind": _TYPE_KIND,
        "__DirectiveLocation": _DIRECTIVE_LOCATION,
    }


def _resolve_enum_values(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> Any:
    values = source.get("enumValues")
    if values is None:
        return None
    if args.get("includeDeprecated"):
        return values
    return [v for v in values if not v.get("isDeprecated")]


class IntrospectionTypes:
    """Process-global singleton holding the built introspection object types."""

    _cache: dict[str, GraphQLNamedType] | None = None

    @classmethod
    def get(cls) -> dict[str, GraphQLNamedType]:
        if cls._cache is None:
            cls._cache = _build()
        return cls._cache


__all__ = ["IntrospectionTypes"]
