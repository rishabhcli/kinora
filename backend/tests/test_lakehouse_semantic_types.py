"""Unit tests for the semantic-layer primitives: types + the arith interpreter."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from app.lakehouse.semantic.arith import compile_expr, evaluate, referenced_names
from app.lakehouse.semantic.types import (
    Aggregation as Agg,
)
from app.lakehouse.semantic.types import (
    And,
    Comparison,
    FieldRef,
    Not,
    Or,
    Predicate,
    TimeGrain,
    and_all,
    evaluate_filter,
    filter_fields,
    grain_rank,
    is_additive,
    is_coarser_or_equal,
    parse_field_ref,
    validate_identifier,
)

# --------------------------------------------------------------------------- #
# Identifiers / field refs
# --------------------------------------------------------------------------- #


def test_valid_identifier() -> None:
    assert validate_identifier("foo_bar1") == "foo_bar1"


@pytest.mark.parametrize("bad", ["", "1abc", "Foo", "a-b", "a.b", "select;"])
def test_invalid_identifier(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(bad)


def test_parse_field_ref() -> None:
    assert parse_field_ref("shots.book_id") == FieldRef(name="book_id", entity="shots")
    assert parse_field_ref("book_id") == FieldRef(name="book_id")


# --------------------------------------------------------------------------- #
# Time grains
# --------------------------------------------------------------------------- #


def test_grain_ordering() -> None:
    assert grain_rank(TimeGrain.HOUR) < grain_rank(TimeGrain.YEAR)
    assert is_coarser_or_equal(TimeGrain.MONTH, TimeGrain.DAY)
    assert not is_coarser_or_equal(TimeGrain.HOUR, TimeGrain.DAY)


def test_additive_aggregations() -> None:
    assert is_additive(Agg.SUM)
    assert is_additive(Agg.COUNT)
    assert not is_additive(Agg.AVERAGE)
    assert not is_additive(Agg.COUNT_DISTINCT)


# --------------------------------------------------------------------------- #
# Filter AST evaluation
# --------------------------------------------------------------------------- #


def test_predicate_validation_in_requires_tuple() -> None:
    with pytest.raises(ValueError):
        Predicate(field=FieldRef(name="x"), op=Comparison.IN, value="scalar")
    with pytest.raises(ValueError):
        Predicate(field=FieldRef(name="x"), op=Comparison.IN, value=())


def test_predicate_validation_null_takes_no_value() -> None:
    with pytest.raises(ValueError):
        Predicate(field=FieldRef(name="x"), op=Comparison.IS_NULL, value=1)


def test_evaluate_simple_predicates() -> None:
    row = {"role": "generator", "n": 5, "flag": None}
    assert evaluate_filter(
        Predicate(field=FieldRef(name="role"), op=Comparison.EQ, value="generator"), row
    )
    assert evaluate_filter(
        Predicate(field=FieldRef(name="n"), op=Comparison.GTE, value=5), row
    )
    assert not evaluate_filter(
        Predicate(field=FieldRef(name="n"), op=Comparison.GT, value=5), row
    )
    assert evaluate_filter(
        Predicate(field=FieldRef(name="flag"), op=Comparison.IS_NULL), row
    )
    assert evaluate_filter(
        Predicate(field=FieldRef(name="role"), op=Comparison.IN, value=("a", "generator")), row
    )


def test_evaluate_composite_and_or_not() -> None:
    row = {"a": 1, "b": 2}
    expr = And(
        (
            Predicate(field=FieldRef(name="a"), op=Comparison.EQ, value=1),
            Or(
                (
                    Predicate(field=FieldRef(name="b"), op=Comparison.EQ, value=9),
                    Not(Predicate(field=FieldRef(name="b"), op=Comparison.EQ, value=9)),
                )
            ),
        )
    )
    assert evaluate_filter(expr, row)


def test_ordered_comparison_with_null_is_false() -> None:
    row = {"n": None}
    assert not evaluate_filter(
        Predicate(field=FieldRef(name="n"), op=Comparison.GT, value=0), row
    )


def test_naive_datetime_coerced_to_utc() -> None:
    row = {"t": datetime(2026, 6, 1, 12)}  # naive
    aware = datetime(2026, 6, 1, 10, tzinfo=UTC)
    assert evaluate_filter(
        Predicate(field=FieldRef(name="t"), op=Comparison.GT, value=aware), row
    )


def test_and_all_flattens_and_drops_none() -> None:
    p1 = Predicate(field=FieldRef(name="a"), op=Comparison.EQ, value=1)
    p2 = Predicate(field=FieldRef(name="b"), op=Comparison.EQ, value=2)
    combined = and_all(None, And((p1,)), p2, None)
    assert isinstance(combined, And)
    assert combined.terms == (p1, p2)
    assert and_all(None, None) is None
    assert and_all(p1) is p1


def test_filter_fields_collects_qualified_names() -> None:
    expr = And(
        (
            Predicate(field=FieldRef(name="a", entity="m"), op=Comparison.EQ, value=1),
            Not(Predicate(field=FieldRef(name="b"), op=Comparison.EQ, value=2)),
        )
    )
    assert filter_fields(expr) == frozenset({"m.a", "b"})


# --------------------------------------------------------------------------- #
# Arithmetic interpreter
# --------------------------------------------------------------------------- #


def test_arith_precedence_and_parens() -> None:
    ast = compile_expr("1 + 2 * 3")
    assert evaluate(ast, {}) == 7
    ast = compile_expr("(1 + 2) * 3")
    assert evaluate(ast, {}) == 9


def test_arith_unary_minus() -> None:
    assert evaluate(compile_expr("-5 + 2"), {}) == -3


def test_arith_variables() -> None:
    ast = compile_expr("(1 - rejected / total) * 100")
    value = evaluate(ast, {"rejected": 10.0, "total": 40.0})
    assert value is not None
    assert math.isclose(value, 75.0)
    assert set(referenced_names(ast)) == {"rejected", "total"}


def test_arith_division_by_zero_is_zero() -> None:
    assert evaluate(compile_expr("a / b"), {"a": 5.0, "b": 0.0}) == 0.0


def test_arith_none_propagates() -> None:
    assert evaluate(compile_expr("a + b"), {"a": None, "b": 3.0}) is None


def test_arith_unbound_name_raises() -> None:
    with pytest.raises(KeyError):
        evaluate(compile_expr("missing"), {})


@pytest.mark.parametrize("bad", ["1 +", "(1 + 2", "1 ** 2", "@", "a b"])
def test_arith_garbage_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        compile_expr(bad)
