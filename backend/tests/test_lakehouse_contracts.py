"""Tests that the concrete classes satisfy the sibling-facing contracts."""

from __future__ import annotations

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.catalog import Catalog
from app.lakehouse.warehouse.contracts import QueryEngine, Table, TableScan
from app.lakehouse.warehouse.engine import WarehouseQueryEngine
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema


def make_table_and_engine() -> tuple[object, WarehouseQueryEngine]:
    cat = Catalog()
    s = Schema.of(Field("id", LogicalType.INT64, nullable=False), Field("v", LogicalType.STRING))
    t = cat.create_table("t", s)
    t.append(
        RecordBatch.from_mapping(
            s,
            {
                "id": ColumnVector.from_pylist(LogicalType.INT64, [1, 2]),
                "v": ColumnVector.from_pylist(LogicalType.STRING, ["a", "b"]),
            },
        )
    )
    return t, WarehouseQueryEngine.from_catalog(cat)


def test_catalog_table_is_table_protocol() -> None:
    table, _ = make_table_and_engine()
    assert isinstance(table, Table)
    # The contract surface is callable.
    assert table.name == "t"
    assert table.schema.names == ["id", "v"]
    assert table.partition_spec.is_unpartitioned
    assert table.current_snapshot_id() is not None
    batches = table.scan(columns=["id"])
    assert all(b.schema.names == ["id"] for b in batches)


def test_engine_is_queryengine_protocol() -> None:
    _, eng = make_table_and_engine()
    assert isinstance(eng, QueryEngine)
    res = eng.execute(eng.scan("t"))
    assert res.num_rows == 2


def test_table_scan_dataclass() -> None:
    ts = TableScan(table_name="t", columns=["id"])
    assert ts.snapshot_id is None
    assert ts.predicate is None
    assert ts.columns == ["id"]
