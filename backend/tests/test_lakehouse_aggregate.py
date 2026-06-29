"""Unit tests for aggregate accumulators and specs."""

from __future__ import annotations

from typing import Any

from app.lakehouse.warehouse.aggregate import (
    AggFunc,
    AggregateSpec,
    avg,
    count,
    count_distinct,
    count_star,
    max_,
    min_,
    sum_,
)
from app.lakehouse.warehouse.expr import col
from app.lakehouse.warehouse.types import LogicalType


def fold(spec: AggregateSpec, cells: list[tuple[Any, bool]]) -> tuple[Any, bool]:
    acc = spec.new_accumulator()
    for value, valid in cells:
        acc.update(value, valid)
    return acc.result()


def test_count_star_counts_nulls() -> None:
    spec = count_star("n")
    assert fold(spec, [(1, True), (None, False), (3, True)]) == (3, True)


def test_count_ignores_nulls() -> None:
    spec = count(col("x"))
    assert fold(spec, [(1, True), (None, False), (3, True)]) == (2, True)


def test_count_distinct() -> None:
    spec = count_distinct(col("x"))
    # {1, 2, 5} distinct present; NULL ignored.
    assert fold(spec, [(1, True), (1, True), (2, True), (5, True), (None, False)]) == (3, True)


def test_sum() -> None:
    spec = sum_(col("x"))
    assert fold(spec, [(1, True), (2, True), (None, False)]) == (3, True)


def test_sum_all_null_is_null() -> None:
    spec = sum_(col("x"))
    assert fold(spec, [(None, False)]) == (0, False)


def test_min_max() -> None:
    assert fold(min_(col("x")), [(3, True), (1, True), (5, True)]) == (1, True)
    assert fold(max_(col("x")), [(3, True), (1, True), (5, True)]) == (5, True)


def test_min_all_null_is_null() -> None:
    assert fold(min_(col("x")), [(None, False)]) == (None, False)


def test_avg() -> None:
    val, ok = fold(avg(col("x")), [(2, True), (4, True), (None, False)])
    assert ok
    assert val == 3.0


def test_avg_empty_is_null() -> None:
    assert fold(avg(col("x")), [(None, False)]) == (None, False)


def test_result_types() -> None:
    types = {"x": LogicalType.INT64, "y": LogicalType.FLOAT64}
    assert count_star().result_type(types) is LogicalType.INT64
    assert count(col("x")).result_type(types) is LogicalType.INT64
    assert avg(col("x")).result_type(types) is LogicalType.FLOAT64
    assert sum_(col("x")).result_type(types) is LogicalType.INT64
    assert sum_(col("y")).result_type(types) is LogicalType.FLOAT64
    assert min_(col("x")).result_type(types) is LogicalType.INT64
    assert max_(col("y")).result_type(types) is LogicalType.FLOAT64


def test_agg_func_enum() -> None:
    assert AggFunc.SUM.value == "sum"
    assert count_star("c").func is AggFunc.COUNT_STAR
