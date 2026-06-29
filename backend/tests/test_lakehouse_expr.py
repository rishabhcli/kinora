"""Unit tests for scalar expressions (vectorized kernels + 3-valued NULL logic)."""

from __future__ import annotations

import pytest

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.expr import (
    Arithmetic,
    ArithOp,
    Cast,
    Coalesce,
    CompareOp,
    Comparison,
    and_,
    col,
    lit,
    not_,
    or_,
)
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema


def make_batch() -> RecordBatch:
    schema = Schema.of(
        Field("a", LogicalType.INT64),
        Field("b", LogicalType.INT64),
        Field("f", LogicalType.FLOAT64),
        Field("flag", LogicalType.BOOL),
    )
    return RecordBatch.from_mapping(
        schema,
        {
            "a": ColumnVector.from_pylist(LogicalType.INT64, [1, 2, None, 4]),
            "b": ColumnVector.from_pylist(LogicalType.INT64, [10, 0, 30, 40]),
            "f": ColumnVector.from_pylist(LogicalType.FLOAT64, [1.5, None, 3.0, 4.0]),
            "flag": ColumnVector.from_pylist(LogicalType.BOOL, [True, False, None, True]),
        },
    )


def test_column_and_literal() -> None:
    b = make_batch()
    assert col("a").evaluate(b).to_pylist() == [1, 2, None, 4]
    assert lit(7).evaluate(b).to_pylist() == [7, 7, 7, 7]


def test_arithmetic_add_null_propagates() -> None:
    b = make_batch()
    res = Arithmetic(ArithOp.ADD, col("a"), col("b")).evaluate(b)
    assert res.to_pylist() == [11, 2, None, 44]


def test_arithmetic_mul_mixed_float() -> None:
    b = make_batch()
    res = Arithmetic(ArithOp.MUL, col("a"), col("f")).evaluate(b)
    assert res.dtype is LogicalType.FLOAT64
    assert res.to_pylist() == [1.5, None, None, 16.0]


def test_arithmetic_div_by_zero_is_null() -> None:
    b = make_batch()
    res = Arithmetic(ArithOp.DIV, col("a"), col("b")).evaluate(b)
    # row 1: 2/0 -> NULL
    assert res.get(1) is None
    assert res.get(0) == 0.1


def test_comparison_three_valued() -> None:
    b = make_batch()
    res = Comparison(CompareOp.GT, col("a"), lit(1)).evaluate(b)
    assert res.dtype is LogicalType.BOOL
    assert res.to_pylist() == [False, True, None, True]  # None where a is NULL


def test_boolop_and_or_not() -> None:
    b = make_batch()
    a_gt0 = Comparison(CompareOp.GT, col("a"), lit(0))
    b_gt20 = Comparison(CompareOp.GT, col("b"), lit(20))
    res_and = and_(a_gt0, b_gt20).evaluate(b)
    # row0: T & F=F; row1: T & F=F; row2: NULL & T = NULL; row3: T & T=T
    assert res_and.to_pylist() == [False, False, None, True]
    res_or = or_(a_gt0, b_gt20).evaluate(b)
    # row2: NULL | T = T
    assert res_or.to_pylist() == [True, True, True, True]
    res_not = not_(a_gt0).evaluate(b)
    assert res_not.to_pylist() == [False, False, None, False]


def test_and_definite_false_dominates_null() -> None:
    schema = Schema.of(Field("a", LogicalType.INT64), Field("b", LogicalType.INT64))
    b = RecordBatch.from_mapping(
        schema,
        {
            "a": ColumnVector.from_pylist(LogicalType.INT64, [None]),
            "b": ColumnVector.from_pylist(LogicalType.INT64, [5]),
        },
    )
    # (a > 0) is NULL, (b > 100) is False -> AND is definite False.
    res = and_(
        Comparison(CompareOp.GT, col("a"), lit(0)),
        Comparison(CompareOp.GT, col("b"), lit(100)),
    ).evaluate(b)
    assert res.to_pylist() == [False]


def test_cast() -> None:
    b = make_batch()
    res = Cast(col("a"), LogicalType.FLOAT64).evaluate(b)
    assert res.dtype is LogicalType.FLOAT64
    assert res.to_pylist() == [1.0, 2.0, None, 4.0]
    res_str = Cast(col("a"), LogicalType.STRING).evaluate(b)
    assert res_str.to_pylist() == ["1", "2", None, "4"]


def test_coalesce() -> None:
    b = make_batch()
    res = Coalesce((col("a"), lit(99))).evaluate(b)
    assert res.to_pylist() == [1, 2, 99, 4]


def test_lit_type_inference() -> None:
    assert lit(True).dtype is LogicalType.BOOL
    assert lit(5).dtype is LogicalType.INT64
    assert lit(5.0).dtype is LogicalType.FLOAT64
    assert lit("x").dtype is LogicalType.STRING
    assert lit(b"x").dtype is LogicalType.BYTES


def test_lit_type_inference_failure() -> None:
    with pytest.raises(TypeError):
        lit(object())


def test_result_type_inference() -> None:
    types = {"a": LogicalType.INT64, "f": LogicalType.FLOAT64}
    assert Arithmetic(ArithOp.ADD, col("a"), col("a")).result_type(types) is LogicalType.INT64
    assert Arithmetic(ArithOp.MUL, col("a"), col("f")).result_type(types) is LogicalType.FLOAT64
    assert Comparison(CompareOp.EQ, col("a"), lit(1)).result_type(types) is LogicalType.BOOL


def test_columns_collection() -> None:
    e = and_(
        Comparison(CompareOp.GT, col("a"), lit(1)),
        Arithmetic(ArithOp.ADD, col("b"), col("a")) and Comparison(CompareOp.LT, col("b"), lit(5)),
    )
    assert "a" in e.columns()
