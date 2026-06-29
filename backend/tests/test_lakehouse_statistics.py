"""Unit tests for column statistics, zonemaps, and stat merging."""

from __future__ import annotations

from app.lakehouse.warehouse.statistics import (
    build_zonemap,
    compute_statistics,
    merge_statistics,
)
from app.lakehouse.warehouse.types import ColumnVector, LogicalType


def test_statistics_basic() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [3, 1, None, 5, 1])
    st = compute_statistics(v)
    assert st.count == 5
    assert st.null_count == 1
    assert st.distinct_count == 3  # {3, 1, 5}
    assert st.min_value == 1
    assert st.max_value == 5
    assert st.has_range
    assert not st.all_null


def test_statistics_all_null() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [None, None])
    st = compute_statistics(v)
    assert st.all_null
    assert not st.has_range
    assert st.min_value is None


def test_statistics_contains_range() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [10, 20, 30])
    st = compute_statistics(v)
    assert st.contains_range(15, 25)
    assert st.contains_range(0, 100)
    assert not st.contains_range(40, 50)
    assert not st.contains_range(-5, 5)


def test_statistics_strings() -> None:
    v = ColumnVector.from_pylist(LogicalType.STRING, ["banana", "apple", "cherry"])
    st = compute_statistics(v)
    assert st.min_value == "apple"
    assert st.max_value == "cherry"


def test_zonemap_ranges() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, list(range(10)))
    zm = build_zonemap(v, zone_size=3)
    assert len(zm.zones) == 4  # 3,3,3,1
    assert zm.zones[0].min_value == 0
    assert zm.zones[0].max_value == 2
    assert zm.zones[3].min_value == 9
    assert zm.zones[3].length == 1


def test_zonemap_overlapping() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, list(range(10)))
    zm = build_zonemap(v, zone_size=3)
    # Looking for [7,8]: only the third zone (6,7,8) overlaps.
    zones = zm.overlapping_zones(7, 8)
    assert len(zones) == 1
    assert zones[0].start_row == 6
    rows = zm.overlapping_rows(7, 8)
    assert rows == [6, 7, 8]


def test_zonemap_nulls_counted() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [1, None, 3, None])
    zm = build_zonemap(v, zone_size=2)
    assert zm.zones[0].null_count == 1
    assert zm.zones[1].null_count == 1


def test_merge_statistics() -> None:
    a = compute_statistics(ColumnVector.from_pylist(LogicalType.INT64, [1, 5, None]))
    b = compute_statistics(ColumnVector.from_pylist(LogicalType.INT64, [3, 9]))
    merged = merge_statistics([a, b])
    assert merged.count == 5
    assert merged.null_count == 1
    assert merged.min_value == 1
    assert merged.max_value == 9
    # distinct is an upper bound (sum across chunks).
    assert merged.distinct_count == a.distinct_count + b.distinct_count
