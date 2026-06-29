"""Unit tests for partition specs and transforms."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.lakehouse.warehouse.partition import (
    PartitionField,
    PartitionSpec,
    Transform,
    partition_key,
)
from app.lakehouse.warehouse.types import Field, LogicalType, Schema


def micros(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1_000_000)


def test_identity_transform() -> None:
    pf = PartitionField("region", Transform.IDENTITY)
    assert pf.apply("us") == "us"
    assert pf.apply(None) is None
    assert pf.partition_name() == "region_identity"


def test_time_transforms() -> None:
    ts = micros(2026, 6, 29, 13)
    assert PartitionField("t", Transform.YEAR).apply(ts) == 2026
    assert PartitionField("t", Transform.MONTH).apply(ts) == 2026 * 12 + 5
    assert PartitionField("t", Transform.DAY).apply(ts) == ts // (1_000_000 * 86_400)
    assert PartitionField("t", Transform.HOUR).apply(ts) == ts // (1_000_000 * 3_600)


def test_bucket_transform_deterministic() -> None:
    pf = PartitionField("user", Transform.BUCKET, param=8)
    a = pf.apply("alice")
    b = pf.apply("alice")
    assert a == b
    assert 0 <= a < 8
    assert pf.partition_name() == "user_bucket_8"


def test_bucket_requires_positive_param() -> None:
    with pytest.raises(ValueError):
        PartitionField("u", Transform.BUCKET, param=0).apply("x")


def test_truncate_int_and_str() -> None:
    assert PartitionField("n", Transform.TRUNCATE, param=10).apply(47) == 40
    assert PartitionField("s", Transform.TRUNCATE, param=3).apply("abcdef") == "abc"


def test_truncate_requires_positive_param() -> None:
    with pytest.raises(ValueError):
        PartitionField("n", Transform.TRUNCATE, param=0).apply(5)


def test_truncate_type_error() -> None:
    with pytest.raises(TypeError):
        PartitionField("f", Transform.TRUNCATE, param=2).apply(1.5)


def test_partition_spec_value() -> None:
    spec = PartitionSpec(
        (
            PartitionField("region", Transform.IDENTITY),
            PartitionField("t", Transform.YEAR),
        )
    )
    row = {"region": "us", "t": micros(2026, 1, 1)}
    assert spec.partition_value(row) == ("us", 2026)
    assert spec.names() == ["region_identity", "t_year"]


def test_partition_spec_validate() -> None:
    schema = Schema.of(
        Field("region", LogicalType.STRING),
        Field("ts", LogicalType.TIMESTAMP),
        Field("n", LogicalType.INT64),
    )
    PartitionSpec((PartitionField("ts", Transform.DAY),)).validate(schema)
    with pytest.raises(KeyError):
        PartitionSpec((PartitionField("missing", Transform.IDENTITY),)).validate(schema)
    with pytest.raises(TypeError):
        # year transform on a non-timestamp column.
        PartitionSpec((PartitionField("n", Transform.YEAR),)).validate(schema)


def test_unpartitioned() -> None:
    spec = PartitionSpec.unpartitioned()
    assert spec.is_unpartitioned
    assert spec.partition_value({"a": 1}) == ()


def test_partition_key_stability() -> None:
    assert partition_key(("us", 2026)) == partition_key(("us", 2026))
    assert partition_key((None,)) == "__NULL__"
    assert partition_key(("us",)) != partition_key(("eu",))


def test_named_partition_field() -> None:
    pf = PartitionField("region", Transform.IDENTITY, name="r")
    assert pf.partition_name() == "r"
