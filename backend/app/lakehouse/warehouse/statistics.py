"""Per-chunk column statistics and zonemaps for predicate pushdown.

When a column chunk is written we capture a small, cheap summary:

* :class:`ColumnStatistics` — value count, null count, distinct count, and the
  ``min`` / ``max`` over the **non-null** values (only for ordered types). These
  drive *predicate pushdown*: a row group whose ``[min, max]`` cannot satisfy a
  filter is skipped without decoding.
* :class:`ZoneMap` — a coarse partition of a chunk into fixed-size *zones*, each
  with its own min/max. A finer skip granularity than a single chunk-level stat
  for clustered/sorted data, at negligible cost.

All statistics are computed from a
:class:`~app.lakehouse.warehouse.types.ColumnVector` and are pure / deterministic.
``min``/``max`` are ``None`` for an all-null chunk or an unordered type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.types import ColumnVector, LogicalType


@dataclass(frozen=True, slots=True)
class ColumnStatistics:
    """A summary of one column chunk used for skipping and planning."""

    dtype: LogicalType
    count: int
    null_count: int
    distinct_count: int
    min_value: Any | None
    max_value: Any | None

    @property
    def all_null(self) -> bool:
        return self.count > 0 and self.null_count == self.count

    @property
    def has_range(self) -> bool:
        return self.min_value is not None and self.max_value is not None

    def contains_range(self, low: Any, high: Any) -> bool:
        """Whether ``[low, high]`` overlaps this chunk's value range (inclusive)."""
        if not self.has_range:
            return False
        return not (high < self.min_value or low > self.max_value)


def compute_statistics(vec: ColumnVector) -> ColumnStatistics:
    """Compute :class:`ColumnStatistics` over a vector (one pass)."""
    nulls = 0
    distinct: set[Any] = set()
    minv: Any | None = None
    maxv: Any | None = None
    ordered = vec.dtype.is_ordered
    for i in range(len(vec)):
        if not vec.is_valid(i):
            nulls += 1
            continue
        val = vec.value(i)
        distinct.add(val)
        if ordered:
            if minv is None or val < minv:
                minv = val
            if maxv is None or val > maxv:
                maxv = val
    return ColumnStatistics(
        dtype=vec.dtype,
        count=len(vec),
        null_count=nulls,
        distinct_count=len(distinct),
        min_value=minv,
        max_value=maxv,
    )


@dataclass(frozen=True, slots=True)
class Zone:
    """A contiguous ``[start_row, start_row + length)`` slice with its own range."""

    start_row: int
    length: int
    null_count: int
    min_value: Any | None
    max_value: Any | None

    @property
    def end_row(self) -> int:
        return self.start_row + self.length

    @property
    def has_range(self) -> bool:
        return self.min_value is not None and self.max_value is not None

    def contains_range(self, low: Any, high: Any) -> bool:
        if not self.has_range:
            return False
        return not (high < self.min_value or low > self.max_value)


@dataclass(frozen=True, slots=True)
class ZoneMap:
    """A list of fixed-size zones over a chunk, for sub-chunk skipping."""

    zone_size: int
    zones: tuple[Zone, ...]

    def overlapping_zones(self, low: Any, high: Any) -> list[Zone]:
        """Zones whose range overlaps ``[low, high]`` (the ones worth scanning)."""
        return [z for z in self.zones if z.contains_range(low, high)]

    def overlapping_rows(self, low: Any, high: Any) -> list[int]:
        """Row indices in zones that overlap ``[low, high]`` (candidate rows)."""
        rows: list[int] = []
        for z in self.overlapping_zones(low, high):
            rows.extend(range(z.start_row, z.end_row))
        return rows


def build_zonemap(vec: ColumnVector, zone_size: int = 1024) -> ZoneMap:
    """Partition ``vec`` into zones of ``zone_size`` rows, each with its range."""
    if zone_size <= 0:
        raise ValueError("zone_size must be positive")
    ordered = vec.dtype.is_ordered
    zones: list[Zone] = []
    n = len(vec)
    for start in range(0, n, zone_size):
        end = min(start + zone_size, n)
        nulls = 0
        minv: Any | None = None
        maxv: Any | None = None
        for i in range(start, end):
            if not vec.is_valid(i):
                nulls += 1
                continue
            if ordered:
                val = vec.value(i)
                if minv is None or val < minv:
                    minv = val
                if maxv is None or val > maxv:
                    maxv = val
        zones.append(
            Zone(
                start_row=start,
                length=end - start,
                null_count=nulls,
                min_value=minv,
                max_value=maxv,
            )
        )
    return ZoneMap(zone_size=zone_size, zones=tuple(zones))


def merge_statistics(stats: list[ColumnStatistics]) -> ColumnStatistics:
    """Combine per-chunk stats into a row-group / file level summary.

    ``distinct_count`` becomes an **upper bound** (sum across chunks) since exact
    cross-chunk distinctness would require the values; for pushdown the range and
    null counts are what matter and those combine exactly.
    """
    if not stats:
        raise ValueError("cannot merge empty statistics")
    dtype = stats[0].dtype
    if any(s.dtype is not dtype for s in stats):
        raise ValueError("cannot merge statistics of differing dtypes")
    count = sum(s.count for s in stats)
    null_count = sum(s.null_count for s in stats)
    distinct = sum(s.distinct_count for s in stats)
    mins = [s.min_value for s in stats if s.min_value is not None]
    maxs = [s.max_value for s in stats if s.max_value is not None]
    return ColumnStatistics(
        dtype=dtype,
        count=count,
        null_count=null_count,
        distinct_count=distinct,
        min_value=min(mins) if mins else None,
        max_value=max(maxs) if maxs else None,
    )


__all__ = [
    "ColumnStatistics",
    "Zone",
    "ZoneMap",
    "build_zonemap",
    "compute_statistics",
    "merge_statistics",
]
