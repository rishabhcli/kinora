"""Unit tests for the schema compatibility checker (no infra)."""

from __future__ import annotations

from app.graphql.compat import (
    ChangeKind,
    breaking_changes,
    diff_schemas,
    is_backward_compatible,
)
from app.graphql.scalars import GraphQLID, GraphQLInt, GraphQLString
from app.graphql.schema import Schema
from app.graphql.type_system import (
    Argument,
    EnumValue,
    Field,
    GraphQLEnum,
    GraphQLNonNull,
    GraphQLObject,
)


def _query(fields: dict[str, Field]) -> GraphQLObject:
    return GraphQLObject("Query", fields)


def test_identical_schema_is_compatible() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString)}))
    b = Schema(query=_query({"x": Field(GraphQLString)}))
    assert is_backward_compatible(a, b)
    assert diff_schemas(a, b) == []


def test_removing_a_field_is_breaking() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString), "y": Field(GraphQLInt)}))
    b = Schema(query=_query({"x": Field(GraphQLString)}))
    changes = breaking_changes(a, b)
    assert any("Query.y" in c.coordinate for c in changes)
    assert not is_backward_compatible(a, b)


def test_adding_a_field_is_safe() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString)}))
    b = Schema(query=_query({"x": Field(GraphQLString), "y": Field(GraphQLInt)}))
    assert is_backward_compatible(a, b)
    changes = diff_schemas(a, b)
    assert any(c.kind is ChangeKind.SAFE and "Query.y" in c.coordinate for c in changes)


def test_changing_output_field_type_is_breaking() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString)}))
    b = Schema(query=_query({"x": Field(GraphQLInt)}))
    assert not is_backward_compatible(a, b)


def test_adding_required_argument_is_breaking() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString)}))
    b = Schema(
        query=_query(
            {"x": Field(GraphQLString, args={"id": Argument(GraphQLNonNull(GraphQLID))})}
        )
    )
    changes = breaking_changes(a, b)
    assert any("Query.x.id" in c.coordinate for c in changes)


def test_adding_optional_argument_is_dangerous_not_breaking() -> None:
    a = Schema(query=_query({"x": Field(GraphQLString)}))
    b = Schema(query=_query({"x": Field(GraphQLString, args={"q": Argument(GraphQLString)})}))
    assert is_backward_compatible(a, b)
    changes = diff_schemas(a, b)
    assert any(c.kind is ChangeKind.DANGEROUS for c in changes)


def test_adding_enum_value_is_dangerous() -> None:
    color_old = GraphQLEnum("Color", [EnumValue("RED", "red")])
    color_new = GraphQLEnum("Color", [EnumValue("RED", "red"), EnumValue("BLUE", "blue")])
    a = Schema(query=_query({"c": Field(color_old)}))
    b = Schema(query=_query({"c": Field(color_new)}))
    changes = diff_schemas(a, b)
    assert any(c.kind is ChangeKind.DANGEROUS and "Color.BLUE" in c.coordinate for c in changes)
    assert is_backward_compatible(a, b)


def test_removing_enum_value_is_breaking() -> None:
    color_old = GraphQLEnum("Color", [EnumValue("RED", "red"), EnumValue("BLUE", "blue")])
    color_new = GraphQLEnum("Color", [EnumValue("RED", "red")])
    a = Schema(query=_query({"c": Field(color_old)}))
    b = Schema(query=_query({"c": Field(color_new)}))
    assert not is_backward_compatible(a, b)


def test_live_schema_is_self_compatible() -> None:
    from app.graphql.root import build_schema

    schema = build_schema()
    assert is_backward_compatible(schema, schema)
