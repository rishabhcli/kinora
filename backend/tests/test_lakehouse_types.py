"""Unit tests for the lakehouse logical type system + column vector (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.lakehouse.warehouse.types import (
    ColumnVector,
    Field,
    LogicalType,
    Schema,
    placeholder_for,
)


def test_logical_type_predicates() -> None:
    assert LogicalType.INT64.is_integer
    assert LogicalType.TIMESTAMP.is_integer
    assert LogicalType.DECIMAL.is_integer
    assert LogicalType.FLOAT64.is_floating
    assert LogicalType.INT64.is_numeric
    assert not LogicalType.STRING.is_numeric
    assert LogicalType.STRING.is_ordered
    assert LogicalType.BYTES.is_ordered


def test_field_validation() -> None:
    with pytest.raises(ValueError):
        Field(name="", dtype=LogicalType.INT64)
    with pytest.raises(ValueError):
        Field(name="x", dtype=LogicalType.INT64, scale=2)  # scale only on DECIMAL
    with pytest.raises(ValueError):
        Field(name="x", dtype=LogicalType.DECIMAL, scale=-1)
    # DECIMAL with scale is fine.
    Field(name="x", dtype=LogicalType.DECIMAL, scale=2)


def test_schema_duplicate_names_rejected() -> None:
    with pytest.raises(ValueError):
        Schema.of(Field("a", LogicalType.INT64), Field("a", LogicalType.STRING))


def test_schema_select_and_index() -> None:
    s = Schema.of(
        Field("a", LogicalType.INT64),
        Field("b", LogicalType.STRING),
        Field("c", LogicalType.BOOL),
    )
    assert s.names == ["a", "b", "c"]
    assert s.index_of("b") == 1
    assert s.has("c")
    assert not s.has("z")
    sub = s.select(["c", "a"])
    assert sub.names == ["c", "a"]
    with pytest.raises(KeyError):
        s.field("z")


def test_schema_with_fields() -> None:
    s = Schema.of(Field("a", LogicalType.INT64))
    s2 = s.with_fields([Field("b", LogicalType.STRING)])
    assert s2.names == ["a", "b"]


def test_column_vector_from_pylist_nulls() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [1, None, 3])
    assert len(v) == 3
    assert v.null_count == 1
    assert v.is_valid(0)
    assert not v.is_valid(1)
    assert v.get(1) is None
    assert v.value(1) == placeholder_for(LogicalType.INT64)
    assert v.to_pylist() == [1, None, 3]


def test_column_vector_coercion_errors() -> None:
    with pytest.raises(TypeError):
        ColumnVector.from_pylist(LogicalType.INT64, [True])  # bool not int
    with pytest.raises(TypeError):
        ColumnVector.from_pylist(LogicalType.BOOL, [1])
    with pytest.raises(TypeError):
        ColumnVector.from_pylist(LogicalType.STRING, [123])


def test_column_vector_timestamp_coercion() -> None:
    dt = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
    v = ColumnVector.from_pylist(LogicalType.TIMESTAMP, [dt, None, 5])
    assert v.value(0) == int(dt.timestamp() * 1_000_000)
    assert v.get(2) == 5


def test_column_vector_naive_datetime_assumed_utc() -> None:
    naive = datetime(2026, 1, 1, 0, 0)
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    vn = ColumnVector.from_pylist(LogicalType.TIMESTAMP, [naive])
    va = ColumnVector.from_pylist(LogicalType.TIMESTAMP, [aware])
    assert vn.value(0) == va.value(0)


def test_column_vector_float_accepts_int() -> None:
    v = ColumnVector.from_pylist(LogicalType.FLOAT64, [1, 2.5, None])
    assert v.value(0) == 1.0
    assert isinstance(v.value(0), float)


def test_column_vector_take_filter_append() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [10, 20, 30, 40])
    assert v.take([3, 1]).to_pylist() == [40, 20]
    assert v.filter_mask([True, False, True, False]).to_pylist() == [10, 30]
    other = ColumnVector.from_pylist(LogicalType.INT64, [50])
    assert v.append(other).to_pylist() == [10, 20, 30, 40, 50]


def test_column_vector_append_type_mismatch() -> None:
    a = ColumnVector.from_pylist(LogicalType.INT64, [1])
    b = ColumnVector.from_pylist(LogicalType.STRING, ["x"])
    with pytest.raises(TypeError):
        a.append(b)


def test_column_vector_filter_mask_length_check() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [1, 2])
    with pytest.raises(ValueError):
        v.filter_mask([True])


def test_column_vector_equality() -> None:
    a = ColumnVector.from_pylist(LogicalType.INT64, [1, None])
    b = ColumnVector.from_pylist(LogicalType.INT64, [1, None])
    c = ColumnVector.from_pylist(LogicalType.INT64, [1, 2])
    assert a == b
    assert a != c
    assert a != "not a vector"


def test_bytes_column() -> None:
    v = ColumnVector.from_pylist(LogicalType.BYTES, [b"abc", bytearray(b"de"), None])
    assert v.value(0) == b"abc"
    assert v.value(1) == b"de"
    assert v.get(2) is None
