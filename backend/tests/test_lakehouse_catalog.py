"""Unit tests for the Iceberg-shaped catalog: snapshots, time-travel, isolation."""

from __future__ import annotations

from itertools import count
from typing import Any

import pytest

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.blobstore import InMemoryBlobStore, content_key
from app.lakehouse.warehouse.catalog import (
    Catalog,
    CatalogTable,
    ConcurrentCommitError,
    SnapshotNotFoundError,
)
from app.lakehouse.warehouse.partition import PartitionField, PartitionSpec, Transform
from app.lakehouse.warehouse.predicate import col_ge, col_lt
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema


def schema() -> Schema:
    return Schema.of(
        Field("id", LogicalType.INT64, nullable=False),
        Field("region", LogicalType.STRING),
        Field("amt", LogicalType.INT64),
    )


def batch(ids: list, regions: list, amts: list) -> RecordBatch:
    s = schema()
    return RecordBatch.from_mapping(
        s,
        {
            "id": ColumnVector.from_pylist(LogicalType.INT64, ids),
            "region": ColumnVector.from_pylist(LogicalType.STRING, regions),
            "amt": ColumnVector.from_pylist(LogicalType.INT64, amts),
        },
    )


def fresh_catalog() -> Catalog:
    clk = count(1000)
    return Catalog(clock=lambda: next(clk))


def total_rows(table: CatalogTable, **kw: Any) -> int:
    return sum(b.num_rows for b in table.scan(**kw))


# -- blobstore -------------------------------------------------------------- #


def test_blobstore_content_addressed() -> None:
    bs = InMemoryBlobStore()
    k1 = bs.put(b"hello")
    k2 = bs.put(b"hello")
    assert k1 == k2  # idempotent
    assert bs.get(k1) == b"hello"
    assert bs.exists(k1)
    assert len(bs) == 1
    bs.delete(k1)
    assert not bs.exists(k1)


def test_blobstore_missing_key() -> None:
    bs = InMemoryBlobStore()
    with pytest.raises(KeyError):
        bs.get("nope")


def test_content_key_deterministic() -> None:
    assert content_key(b"x") == content_key(b"x")


# -- catalog basics --------------------------------------------------------- #


def test_create_and_list_tables() -> None:
    cat = fresh_catalog()
    cat.create_table("a", schema())
    cat.create_table("b", schema())
    assert cat.list_tables() == ["a", "b"]
    assert cat.has_table("a")
    cat.drop_table("a")
    assert not cat.has_table("a")


def test_create_duplicate_rejected() -> None:
    cat = fresh_catalog()
    cat.create_table("a", schema())
    with pytest.raises(ValueError):
        cat.create_table("a", schema())
    # if_not_exists returns existing.
    same = cat.create_table("a", schema(), if_not_exists=True)
    assert same.name == "a"


def test_missing_table_raises() -> None:
    cat = fresh_catalog()
    with pytest.raises(KeyError):
        cat.table("ghost")


# -- snapshots + appends ---------------------------------------------------- #


def test_append_creates_snapshots() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    s1 = t.append(batch([1, 2], ["us", "eu"], [10, 20]))
    s2 = t.append(batch([3], ["ap"], [30]))
    assert s1.snapshot_id == 1
    assert s2.snapshot_id == 2
    assert s2.parent_id == 1
    assert total_rows(t) == 3
    assert len(t.history()) == 2


def test_overwrite_replaces() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    t.append(batch([1, 2], ["us", "eu"], [10, 20]))
    t.overwrite(batch([9], ["zz"], [99]))
    assert total_rows(t) == 1
    rows = [r for b in t.scan() for r in b.rows()]
    assert rows[0]["id"] == 9


# -- time travel ------------------------------------------------------------ #


def test_time_travel_by_snapshot_id() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    s1 = t.append(batch([1], ["us"], [10]))
    t.append(batch([2, 3], ["eu", "ap"], [20, 30]))
    assert total_rows(t) == 3
    assert total_rows(t, snapshot_id=s1.snapshot_id) == 1


def test_time_travel_by_timestamp() -> None:
    clk = count(1000, 1000)
    cat = Catalog(clock=lambda: next(clk))
    t = cat.create_table("sales", schema())
    s1 = t.append(batch([1], ["us"], [10]))  # ts 1000
    t.append(batch([2], ["eu"], [20]))  # ts 2000
    # As of just after s1.
    got = sum(b.num_rows for b in t.scan_as_of_timestamp(s1.timestamp_ms + 1))
    assert got == 1
    # Before any snapshot.
    assert sum(b.num_rows for b in t.scan_as_of_timestamp(0)) == 0


def test_missing_snapshot_raises() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    t.append(batch([1], ["us"], [10]))
    with pytest.raises(SnapshotNotFoundError):
        t.metadata.snapshot(999)


# -- delete + rollback ------------------------------------------------------ #


def test_delete_where() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema(), rows_per_group=2)
    t.append(batch([1, 2, 3, 4], ["us", "eu", "ap", "us"], [10, 40, 50, 5]))
    snap = t.delete_where(col_ge("amt", 40))
    assert total_rows(t) == 2  # 10 and 5 remain
    assert snap.summary["delta_rows"] == "-2"
    remaining = sorted(r["amt"] for b in t.scan() for r in b.rows())
    assert remaining == [5, 10]


def test_delete_keeps_nonoverlapping_files() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema(), rows_per_group=2)
    t.append(batch([1, 2, 3, 4], ["a", "b", "c", "d"], [1, 2, 100, 200]))
    # Delete amt < 50: removes first group rows, keeps second untouched.
    t.delete_where(col_lt("amt", 50))
    assert total_rows(t) == 2


def test_rollback() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    s1 = t.append(batch([1], ["us"], [10]))
    t.append(batch([2, 3], ["eu", "ap"], [20, 30]))
    assert total_rows(t) == 3
    t.rollback_to(s1.snapshot_id)
    assert total_rows(t) == 1
    assert t.history()[-1].summary["operation"] == "rollback"


# -- partitioning ----------------------------------------------------------- #


def test_partitioned_table_writes_files_per_partition() -> None:
    cat = fresh_catalog()
    spec = PartitionSpec((PartitionField("region", Transform.IDENTITY),))
    t = cat.create_table("sales", schema(), partition_spec=spec)
    snap = t.append(batch([1, 2, 3, 4], ["us", "eu", "us", "ap"], [10, 20, 30, 40]))
    # us, eu, ap -> three data files.
    assert len(snap.data_files) == 3
    parts = sorted(df.partition for df in snap.data_files)
    assert parts == [("ap",), ("eu",), ("us",)]


def test_partition_pruning_query() -> None:
    cat = fresh_catalog()
    spec = PartitionSpec((PartitionField("region", Transform.IDENTITY),))
    t = cat.create_table("sales", schema(), partition_spec=spec)
    t.append(batch([1, 2, 3], ["us", "eu", "us"], [10, 20, 30]))
    from app.lakehouse.warehouse.predicate import col_eq

    rows = [r for b in t.scan(predicate=col_eq("region", "us")) for r in b.rows()]
    assert len(rows) == 2


# -- snapshot isolation ----------------------------------------------------- #


def test_expire_snapshots() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    t.append(batch([1], ["us"], [10]))
    t.append(batch([2], ["eu"], [20]))
    t.append(batch([3], ["ap"], [30]))
    expired = t.expire_snapshots(keep_last=1)
    # Current always retained; first two expired.
    assert 1 in expired
    assert total_rows(t) == 3  # current data intact


def test_concurrent_commit_detection() -> None:
    # Two table handles over the same metadata name simulate two writers; the
    # second commit against a stale base raises.
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    t.append(batch([1], ["us"], [10]))
    # Manually craft a stale commit by calling the internal commit with a wrong base.
    with pytest.raises(ConcurrentCommitError):
        t._commit(  # noqa: SLF001 - testing the isolation guard directly
            base_id=999,
            files=[],
            operation="append",
            delta_rows=0,
            summary=None,
        )


def test_empty_table_scan() -> None:
    cat = fresh_catalog()
    t = cat.create_table("sales", schema())
    batches = t.scan()
    assert sum(b.num_rows for b in batches) == 0
    assert t.current_snapshot_id() is None
