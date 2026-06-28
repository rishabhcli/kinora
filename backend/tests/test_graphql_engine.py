"""Unit tests for the GraphQL engine: type system, executor, validator, scalars.

Builds a small, infra-free schema inline and drives the executor/validator
directly so the engine is exercised without the Kinora domain or any network.
"""

from __future__ import annotations

import pytest

from app.graphql.errors import ErrorCode, GraphQLError
from app.graphql.execute import execute
from app.graphql.language import parse
from app.graphql.scalars import (
    GraphQLBoolean,
    GraphQLID,
    GraphQLInt,
    GraphQLString,
)
from app.graphql.schema import Schema, SchemaError
from app.graphql.type_system import (
    Argument,
    EnumValue,
    Field,
    GraphQLEnum,
    GraphQLInputObject,
    GraphQLInterface,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
    InputField,
    coerce_input,
)
from app.graphql.validate import ValidationLimits, validate

# --------------------------------------------------------------------------- #
# A small fixture schema
# --------------------------------------------------------------------------- #

_COLOR = GraphQLEnum(
    "Color", [EnumValue("RED", "red"), EnumValue("BLUE", "blue")]
)

_NODE = GraphQLInterface(
    "Node",
    lambda: {"id": Field(GraphQLNonNull(GraphQLID))},
    resolve_type=lambda v: "Widget",
)

_WIDGET = GraphQLObject(
    "Widget",
    lambda: {
        "id": Field(GraphQLNonNull(GraphQLID)),
        "name": Field(GraphQLString),
        "color": Field(_COLOR),
        "tags": Field(GraphQLList(GraphQLNonNull(GraphQLString))),
        "broken": Field(
            GraphQLNonNull(GraphQLString),
            resolver=_raise,  # type: ignore[name-defined]
        ),
    },
    interfaces=[_NODE],
)


def _raise(source, args, ctx, info):  # type: ignore[no-untyped-def]
    raise RuntimeError("secret internal detail")


_WIDGET_INPUT = GraphQLInputObject(
    "WidgetInput",
    {
        "name": InputField(GraphQLNonNull(GraphQLString)),
        "count": InputField(GraphQLInt, default_value=1),
    },
)


def _resolve_widget(source, args, ctx, info):  # type: ignore[no-untyped-def]
    return {"id": args["id"], "name": f"w-{args['id']}", "color": "red", "tags": ["a", "b"]}


def _resolve_widgets(source, args, ctx, info):  # type: ignore[no-untyped-def]
    n = args.get("count") or 2
    return [{"id": str(i), "name": f"w-{i}", "color": "blue", "tags": []} for i in range(n)]


async def _resolve_echo(source, args, ctx, info):  # type: ignore[no-untyped-def]
    return args["input"]["name"]


_QUERY = GraphQLObject(
    "Query",
    lambda: {
        "widget": Field(
            _WIDGET,
            args={"id": Argument(GraphQLNonNull(GraphQLID))},
            resolver=_resolve_widget,
        ),
        "widgets": Field(
            GraphQLNonNull(GraphQLList(GraphQLNonNull(_WIDGET))),
            args={"count": Argument(GraphQLInt)},
            resolver=_resolve_widgets,
            cost=1,
            list_cost_multiplier=True,
        ),
        "node": Field(_NODE, args={"id": Argument(GraphQLNonNull(GraphQLID))},
                      resolver=_resolve_widget),
        "boom": Field(_WIDGET, resolver=_resolve_widget),
    },
)

_MUTATION = GraphQLObject(
    "Mutation",
    {
        "echo": Field(
            GraphQLNonNull(GraphQLString),
            args={"input": Argument(GraphQLNonNull(_WIDGET_INPUT))},
            resolver=_resolve_echo,
        )
    },
)

SCHEMA = Schema(query=_QUERY, mutation=_MUTATION)


# --------------------------------------------------------------------------- #
# Scalars + input coercion
# --------------------------------------------------------------------------- #


def test_scalar_serialize_and_parse() -> None:
    assert GraphQLInt.serialize(3) == 3
    assert GraphQLInt.parse_value(7) == 7
    with pytest.raises(GraphQLError):
        GraphQLInt.parse_value("nope")
    with pytest.raises(GraphQLError):
        GraphQLInt.parse_value(2**40)  # outside 32-bit range
    assert GraphQLBoolean.parse_value(True) is True


def test_enum_coercion() -> None:
    assert _COLOR.parse_value("RED") == "red"
    assert _COLOR.serialize("blue") == "BLUE"
    with pytest.raises(GraphQLError):
        _COLOR.parse_value("GREEN")


def test_coerce_input_object_and_defaults() -> None:
    out = coerce_input(_WIDGET_INPUT, {"name": "x"})
    assert out == {"name": "x", "count": 1}
    with pytest.raises(GraphQLError):
        coerce_input(GraphQLNonNull(GraphQLString), None)
    with pytest.raises(GraphQLError):
        coerce_input(_WIDGET_INPUT, {"name": "x", "bogus": 1})


def test_schema_collects_types() -> None:
    assert SCHEMA.get_type("Widget") is _WIDGET
    assert SCHEMA.get_type("Color") is _COLOR
    assert SCHEMA.get_type("WidgetInput") is _WIDGET_INPUT
    # The interface implementation is indexed.
    assert any(o.name == "Widget" for o in SCHEMA.implementations["Node"])


def test_schema_rejects_duplicate_type_name() -> None:
    dupe = GraphQLObject("Widget", {"x": Field(GraphQLString)})
    with pytest.raises(SchemaError):
        Schema(query=GraphQLObject("Query", {"a": Field(dupe)}), extra_types=[_WIDGET])


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def test_execute_basic_query() -> None:
    doc = parse('{ widget(id: "7") { id name color tags } }')
    result = await execute(SCHEMA, doc)
    assert not result.errors
    assert result.data == {
        "widget": {"id": "7", "name": "w-7", "color": "RED", "tags": ["a", "b"]}
    }


async def test_execute_with_variables() -> None:
    doc = parse("query($id: ID!) { widget(id: $id) { id } }")
    result = await execute(SCHEMA, doc, variables={"id": "42"})
    assert result.data == {"widget": {"id": "42"}}


async def test_execute_mutation_with_input_object() -> None:
    doc = parse('mutation { echo(input: {name: "hi"}) }')
    result = await execute(SCHEMA, doc)
    assert result.data == {"echo": "hi"}


async def test_input_object_variable_not_double_coerced() -> None:
    # A whole-argument variable carrying an enum is coerced ONCE (at the variables
    # stage); re-coercing the internal value would fail the enum's parse. Regression
    # test for the executor's variable/arg double-coercion bug.
    doc = parse("mutation($i: WidgetInput!) { echo(input: $i) }")
    result = await execute(SCHEMA, doc, variables={"i": {"name": "z", "count": 2}})
    assert not result.errors
    assert result.data == {"echo": "z"}


async def test_enum_argument_via_variable() -> None:
    # An enum passed as a variable must round-trip through one coercion only.
    doc = parse("query($c: Color) { widgets { id } }")  # widgets ignores color, but parses
    result = await execute(SCHEMA, doc, variables={"c": "RED"})
    assert not result.errors


async def test_execute_interface_via_node() -> None:
    doc = parse('{ node(id: "1") { id ... on Widget { name } } }')
    result = await execute(SCHEMA, doc)
    assert result.data == {"node": {"id": "1", "name": "w-1"}}


async def test_execute_aliases_and_typename() -> None:
    doc = parse('{ a: widget(id: "1") { __typename id } }')
    result = await execute(SCHEMA, doc)
    assert result.data == {"a": {"__typename": "Widget", "id": "1"}}


async def test_resolver_error_is_masked_and_nulls_field() -> None:
    doc = parse('{ boom { id broken } }')
    result = await execute(SCHEMA, doc)
    # `broken` is non-null and raised → its parent object `boom` is nulled.
    assert result.data == {"boom": None}
    assert result.errors
    err = result.errors[0]
    # The internal exception message must NOT leak.
    assert "secret internal detail" not in err.message
    assert err.code == ErrorCode.INTERNAL_SERVER_ERROR


async def test_skip_and_include_directives() -> None:
    doc = parse('{ widget(id: "1") { id name @skip(if: true) } }')
    result = await execute(SCHEMA, doc)
    assert result.data == {"widget": {"id": "1"}}
    doc2 = parse("query($show: Boolean!) { widget(id: \"1\") { id name @include(if: $show) } }")
    result2 = await execute(SCHEMA, doc2, variables={"show": False})
    assert result2.data == {"widget": {"id": "1"}}


async def test_missing_required_variable_errors() -> None:
    doc = parse("query($id: ID!) { widget(id: $id) { id } }")
    result = await execute(SCHEMA, doc, variables={})
    assert result.data is None
    assert result.errors[0].code == ErrorCode.BAD_USER_INPUT


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_validate_unknown_field() -> None:
    errs = validate(SCHEMA, parse('{ widget(id: "1") { nope } }'))
    assert errs and errs[0].code == ErrorCode.GRAPHQL_VALIDATION_FAILED


def test_validate_scalar_subselection() -> None:
    errs = validate(SCHEMA, parse('{ widget(id: "1") { id { x } } }'))
    assert errs


def test_validate_missing_required_argument() -> None:
    errs = validate(SCHEMA, parse('{ widget { id } }'))
    assert any("required argument" in e.message for e in errs)


def test_validate_unknown_argument() -> None:
    errs = validate(SCHEMA, parse('{ widget(id: "1", bogus: 2) { id } }'))
    assert any("Unknown argument" in e.message for e in errs)


def test_validate_depth_limit() -> None:
    # Build a query deeper than a tiny limit.
    query = '{ widget(id: "1") { id } }'
    errs = validate(SCHEMA, parse(query), limits=ValidationLimits(max_depth=0))
    assert any(e.code == ErrorCode.DEPTH_LIMIT_EXCEEDED for e in errs)


def test_validate_complexity_limit() -> None:
    # widgets is list_cost_multiplier; with a big `first` the cost explodes.
    query = '{ widgets(count: 5) { id name } }'
    errs = validate(SCHEMA, parse(query), limits=ValidationLimits(max_cost=1))
    assert any(e.code == ErrorCode.COMPLEXITY_LIMIT_EXCEEDED for e in errs)


def test_validate_clean_query_passes() -> None:
    assert validate(SCHEMA, parse('{ widget(id: "1") { id name } }')) == []


def test_validate_fragment_cycle() -> None:
    doc = parse(
        """
        { widget(id: "1") { ...A } }
        fragment A on Widget { ...B }
        fragment B on Widget { ...A }
        """
    )
    errs = validate(SCHEMA, doc)
    assert any("cycle" in e.message for e in errs)
