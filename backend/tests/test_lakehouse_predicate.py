"""Unit tests for the pushdown predicate algebra (eval + statistics skipping)."""

from __future__ import annotations

from app.lakehouse.warehouse.predicate import (
    And,
    CompareOp,
    IsNotNull,
    IsNull,
    Not,
    Or,
    col_eq,
    col_ge,
    col_gt,
    col_in,
    col_le,
    col_lt,
    col_ne,
)
from app.lakehouse.warehouse.statistics import compute_statistics
from app.lakehouse.warehouse.types import ColumnVector, LogicalType


def batch(items: list, dtype: LogicalType = LogicalType.INT64) -> dict[str, ColumnVector]:
    return {"x": ColumnVector.from_pylist(dtype, items)}


def stats(items: list, dtype: LogicalType = LogicalType.INT64) -> dict:
    return {"x": compute_statistics(ColumnVector.from_pylist(dtype, items))}


def test_compare_evaluate() -> None:
    b = batch([1, 2, 3, None, 5])
    assert col_gt("x", 2).evaluate(b) == [False, False, True, False, True]
    assert col_eq("x", 2).evaluate(b) == [False, True, False, False, False]
    assert col_ne("x", 2).evaluate(b) == [True, False, True, False, True]
    assert col_le("x", 2).evaluate(b) == [True, True, False, False, False]
    assert col_ge("x", 3).evaluate(b) == [False, False, True, False, True]
    assert col_lt("x", 2).evaluate(b) == [True, False, False, False, False]


def test_null_never_passes() -> None:
    b = batch([None, None])
    assert col_eq("x", 0).evaluate(b) == [False, False]


def test_inlist_evaluate() -> None:
    b = batch([1, 2, 3, None])
    assert col_in("x", [1, 3]).evaluate(b) == [True, False, True, False]


def test_isnull_isnotnull() -> None:
    b = batch([1, None, 3])
    assert IsNull("x").evaluate(b) == [False, True, False]
    assert IsNotNull("x").evaluate(b) == [True, False, True]


def test_and_or_not_eval() -> None:
    b = batch([1, 2, 3, 4, 5])
    pred = And((col_ge("x", 2), col_le("x", 4)))
    assert pred.evaluate(b) == [False, True, True, True, False]
    pred2 = Or((col_lt("x", 2), col_gt("x", 4)))
    assert pred2.evaluate(b) == [True, False, False, False, True]
    assert Not(col_eq("x", 3)).evaluate(b) == [True, True, False, True, True]


def test_operator_overloads() -> None:
    b = batch([1, 2, 3])
    pred = col_ge("x", 2) & col_le("x", 2)
    assert pred.evaluate(b) == [False, True, False]
    pred2 = col_lt("x", 2) | col_gt("x", 2)
    assert pred2.evaluate(b) == [True, False, True]
    assert (~col_eq("x", 2)).evaluate(b) == [True, False, True]


def test_pushdown_eq_skips() -> None:
    s = stats([10, 20, 30])  # range [10,30]
    assert col_eq("x", 5).can_skip_statistics(s)  # 5 < min
    assert col_eq("x", 99).can_skip_statistics(s)  # 99 > max
    assert not col_eq("x", 20).can_skip_statistics(s)


def test_pushdown_range_skips() -> None:
    s = stats([10, 20, 30])
    assert col_lt("x", 10).can_skip_statistics(s)  # nothing < 10
    assert col_le("x", 9).can_skip_statistics(s)
    assert col_gt("x", 30).can_skip_statistics(s)  # nothing > 30
    assert col_ge("x", 31).can_skip_statistics(s)
    assert not col_gt("x", 20).can_skip_statistics(s)


def test_pushdown_ne_only_skips_constant() -> None:
    const = stats([5, 5, 5])
    assert col_ne("x", 5).can_skip_statistics(const)
    varied = stats([5, 6])
    assert not col_ne("x", 5).can_skip_statistics(varied)


def test_pushdown_inlist_skips_when_all_outside() -> None:
    s = stats([10, 20, 30])
    assert col_in("x", [1, 2, 100]).can_skip_statistics(s)
    assert not col_in("x", [1, 20]).can_skip_statistics(s)
    assert col_in("x", []).can_skip_statistics(s)  # empty IN matches nothing


def test_pushdown_all_null_skips_compares() -> None:
    s = stats([None, None])
    assert col_eq("x", 1).can_skip_statistics(s)
    assert IsNotNull("x").can_skip_statistics(s)
    assert not IsNull("x").can_skip_statistics(s)


def test_pushdown_isnull_skips_when_no_nulls() -> None:
    s = stats([1, 2, 3])
    assert IsNull("x").can_skip_statistics(s)
    assert not IsNotNull("x").can_skip_statistics(s)


def test_pushdown_and_skips_if_any_conjunct() -> None:
    s = stats([10, 20, 30])
    # gt 100 skips, so the whole AND skips.
    assert And((col_gt("x", 100), col_lt("x", 25))).can_skip_statistics(s)
    assert not And((col_gt("x", 5), col_lt("x", 25))).can_skip_statistics(s)


def test_pushdown_or_skips_only_if_all() -> None:
    s = stats([10, 20, 30])
    assert Or((col_gt("x", 100), col_lt("x", 5))).can_skip_statistics(s)
    assert not Or((col_gt("x", 100), col_eq("x", 20))).can_skip_statistics(s)


def test_pushdown_missing_stats_never_skips() -> None:
    assert not col_eq("y", 1).can_skip_statistics(stats([1, 2]))


def test_compare_op_enum_values() -> None:
    assert CompareOp.EQ.value == "="
    assert CompareOp.GE.value == ">="
