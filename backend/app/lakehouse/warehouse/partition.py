"""Partition specs and partition-value derivation (Iceberg-shaped, simplified).

A :class:`PartitionSpec` is an ordered list of :class:`PartitionField` transforms
applied to source columns to derive a partition tuple per row. Files in the
catalog carry their partition tuple so the query engine can prune whole partitions
before touching any row-group statistics.

Supported transforms (deterministic, pure):

* ``identity`` — the value itself.
* ``year`` / ``month`` / ``day`` / ``hour`` — truncate a TIMESTAMP (int micros) to
  a calendar boundary, expressed as an integer (``2026``, ``2026 * 12 + month``,
  days-since-epoch, hours-since-epoch) so partition values stay comparable ints.
* ``bucket[N]`` — ``hash(value) mod N`` for hash-distributing high-cardinality keys.
* ``truncate[W]`` — integer floor to a width ``W`` (numeric) or string prefix of
  length ``W``.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.lakehouse.warehouse.types import LogicalType, Schema


class Transform(enum.StrEnum):
    IDENTITY = "identity"
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    BUCKET = "bucket"
    TRUNCATE = "truncate"


_TIME_TRANSFORMS = (Transform.YEAR, Transform.MONTH, Transform.DAY, Transform.HOUR)


@dataclass(frozen=True, slots=True)
class PartitionField:
    """One column-to-partition transform.

    ``param`` carries the bucket count (``bucket``) or the truncation width
    (``truncate``); it is ignored for the others. ``name`` is the partition column
    name (defaults to ``<source>_<transform>``).
    """

    source_column: str
    transform: Transform
    param: int = 0
    name: str = ""

    def partition_name(self) -> str:
        if self.name:
            return self.name
        if self.transform in (Transform.BUCKET, Transform.TRUNCATE):
            return f"{self.source_column}_{self.transform}_{self.param}"
        return f"{self.source_column}_{self.transform}"

    def apply(self, value: Any) -> Any:
        """Derive this field's partition value from a source value (NULL→None)."""
        if value is None:
            return None
        t = self.transform
        if t is Transform.IDENTITY:
            return value
        if t in _TIME_TRANSFORMS:
            return _time_transform(t, int(value))
        if t is Transform.BUCKET:
            if self.param <= 0:
                raise ValueError("bucket transform requires a positive param")
            return _bucket(value) % self.param
        if t is Transform.TRUNCATE:
            if self.param <= 0:
                raise ValueError("truncate transform requires a positive param")
            if isinstance(value, str):
                return value[: self.param]
            if isinstance(value, int):
                return (value // self.param) * self.param
            raise TypeError("truncate applies to int or str only")
        raise ValueError(f"unsupported transform {t}")  # pragma: no cover


def _time_transform(t: Transform, micros: int) -> int:
    dt = datetime.fromtimestamp(micros / 1_000_000, tz=UTC)
    if t is Transform.YEAR:
        return dt.year
    if t is Transform.MONTH:
        return dt.year * 12 + (dt.month - 1)
    if t is Transform.DAY:
        return micros // (1_000_000 * 86_400)
    return micros // (1_000_000 * 3_600)  # HOUR


def _bucket(value: Any) -> int:
    """A stable non-cryptographic-but-deterministic hash for bucketing."""
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, bool):
        raw = b"\x01" if value else b"\x00"
    elif isinstance(value, int):
        raw = str(value).encode("ascii")
    elif isinstance(value, float):
        raw = repr(value).encode("ascii")
    else:  # pragma: no cover - defensive
        raw = repr(value).encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big")


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    """An ordered list of partition transforms over a table's columns."""

    fields: tuple[PartitionField, ...] = ()

    @classmethod
    def unpartitioned(cls) -> PartitionSpec:
        return cls(())

    @property
    def is_unpartitioned(self) -> bool:
        return not self.fields

    def names(self) -> list[str]:
        return [f.partition_name() for f in self.fields]

    def validate(self, schema: Schema) -> None:
        for pf in self.fields:
            if not schema.has(pf.source_column):
                raise KeyError(f"partition source column {pf.source_column} not in schema")
            fld = schema.field(pf.source_column)
            if pf.transform in _TIME_TRANSFORMS and fld.dtype is not LogicalType.TIMESTAMP:
                raise TypeError(
                    f"time transform {pf.transform} requires a TIMESTAMP column"
                )

    def partition_value(self, row: dict[str, Any]) -> tuple[Any, ...]:
        """The partition tuple for a logical row (``{column -> value}``)."""
        return tuple(pf.apply(row.get(pf.source_column)) for pf in self.fields)


def partition_key(values: tuple[Any, ...]) -> str:
    """A stable, hashable string key for a partition tuple (catalog grouping)."""
    return "/".join("__NULL__" if v is None else f"{type(v).__name__}:{v}" for v in values)


__all__ = [
    "PartitionField",
    "PartitionSpec",
    "Transform",
    "partition_key",
]
