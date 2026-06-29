"""Unit tests for chunks, row groups, batches, and the columnar file format."""

from __future__ import annotations

import pytest

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.column_chunk import ColumnChunk
from app.lakehouse.warehouse.columnar import ColumnarFile
from app.lakehouse.warehouse.encoding import Encoding
from app.lakehouse.warehouse.predicate import IsNull, col_eq, col_ge, col_lt
from app.lakehouse.warehouse.row_group import RowGroup
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema


def make_schema() -> Schema:
    return Schema.of(
        Field("id", LogicalType.INT64, nullable=False),
        Field("name", LogicalType.STRING),
        Field("score", LogicalType.FLOAT64),
    )


def make_columns(n: int) -> dict[str, ColumnVector]:
    return {
        "id": ColumnVector.from_pylist(LogicalType.INT64, list(range(n))),
        "name": ColumnVector.from_pylist(LogicalType.STRING, [f"u{i % 4}" for i in range(n)]),
        "score": ColumnVector.from_pylist(
            LogicalType.FLOAT64, [float(i) if i % 5 else None for i in range(n)]
        ),
    }


# -- ColumnChunk ------------------------------------------------------------ #


def test_column_chunk_roundtrip() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [1, 2, None, 4])
    chunk = ColumnChunk.write("x", v)
    assert chunk.read() == v
    assert chunk.num_rows == 4
    assert chunk.statistics.min_value == 1
    assert chunk.statistics.max_value == 4


def test_column_chunk_serialize() -> None:
    v = ColumnVector.from_pylist(LogicalType.STRING, ["a", "b", "a"])
    chunk = ColumnChunk.write("name", v)
    raw = chunk.serialize()
    back = ColumnChunk.deserialize(raw)
    assert back.name == "name"
    assert back.read() == v


def test_column_chunk_explicit_encoding() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [7, 7, 7])
    chunk = ColumnChunk.write("x", v, encoding=Encoding.PLAIN)
    assert chunk.encoding is Encoding.PLAIN
    assert chunk.read() == v


# -- RowGroup --------------------------------------------------------------- #


def test_row_group_write_read() -> None:
    schema = make_schema()
    rg = RowGroup.write(schema, make_columns(6))
    assert rg.num_rows == 6
    batch = rg.read()
    assert set(batch.keys()) == {"id", "name", "score"}
    assert batch["id"].to_pylist() == list(range(6))


def test_row_group_projection() -> None:
    schema = make_schema()
    rg = RowGroup.write(schema, make_columns(4))
    proj = rg.read(["id"])
    assert list(proj.keys()) == ["id"]


def test_row_group_ragged_rejected() -> None:
    schema = make_schema()
    cols = make_columns(4)
    cols["id"] = ColumnVector.from_pylist(LogicalType.INT64, [1, 2])
    with pytest.raises(ValueError):
        RowGroup.write(schema, cols)


def test_row_group_dtype_mismatch_rejected() -> None:
    schema = make_schema()
    cols = make_columns(3)
    cols["id"] = ColumnVector.from_pylist(LogicalType.STRING, ["a", "b", "c"])
    with pytest.raises(TypeError):
        RowGroup.write(schema, cols)


def test_row_group_skip() -> None:
    schema = make_schema()
    rg = RowGroup.write(schema, make_columns(10))  # id in [0,9]
    assert rg.can_skip(col_ge("id", 100))
    assert not rg.can_skip(col_ge("id", 5))


# -- RecordBatch ------------------------------------------------------------ #


def test_record_batch_basics() -> None:
    schema = make_schema()
    b = RecordBatch.from_mapping(schema, make_columns(5))
    assert b.num_rows == 5
    assert b.num_columns == 3
    assert b.column("id").to_pylist() == list(range(5))
    rows = b.rows()
    assert rows[0]["id"] == 0


def test_record_batch_project_filter_take() -> None:
    schema = make_schema()
    b = RecordBatch.from_mapping(schema, make_columns(5))
    proj = b.project(["id", "name"])
    assert proj.schema.names == ["id", "name"]
    filt = b.filter_mask([True, False, True, False, True])
    assert filt.num_rows == 3
    taken = b.take([4, 0])
    assert taken.column("id").to_pylist() == [4, 0]


def test_record_batch_slice_concat() -> None:
    schema = make_schema()
    b = RecordBatch.from_mapping(schema, make_columns(6))
    s = b.slice(2, 3)
    assert s.column("id").to_pylist() == [2, 3, 4]
    cc = RecordBatch.concat([b.slice(0, 2), b.slice(2, 2)])
    assert cc.column("id").to_pylist() == [0, 1, 2, 3]


def test_record_batch_with_column() -> None:
    schema = make_schema()
    b = RecordBatch.from_mapping(schema, make_columns(3))
    extra = ColumnVector.from_pylist(LogicalType.BOOL, [True, False, True])
    b2 = b.with_column(Field("flag", LogicalType.BOOL), extra)
    assert "flag" in b2.schema.names
    # Replace existing column.
    repl = ColumnVector.from_pylist(LogicalType.INT64, [9, 9, 9])
    b3 = b.with_column(Field("id", LogicalType.INT64, nullable=False), repl)
    assert b3.column("id").to_pylist() == [9, 9, 9]


def test_record_batch_concat_schema_mismatch() -> None:
    a = RecordBatch.from_mapping(make_schema(), make_columns(2))
    other = Schema.of(Field("id", LogicalType.INT64))
    b = RecordBatch.from_mapping(other, {"id": ColumnVector.from_pylist(LogicalType.INT64, [1])})
    with pytest.raises(ValueError):
        RecordBatch.concat([a, b])


# -- ColumnarFile ----------------------------------------------------------- #


def test_columnar_file_roundtrip_serialize() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(100), rows_per_group=25)
    assert f.num_row_groups == 4
    assert f.num_rows == 100
    blob = f.serialize()
    f2 = ColumnarFile.deserialize(blob)
    assert f2.read_all() == f.read_all()


def test_columnar_file_bad_magic() -> None:
    with pytest.raises(ValueError):
        ColumnarFile.deserialize(b"not a file at all....")


def test_columnar_file_predicate_pushdown_skips_groups() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(100), rows_per_group=25)
    pred = col_ge("id", 60)
    rows = sum(b.num_rows for b in f.scan(predicate=pred))
    assert rows == 40
    skipped = sum(1 for g in f.row_groups if g.can_skip(pred))
    assert skipped >= 2  # first two groups (0-24, 25-49) skipped


def test_columnar_file_projection() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(10), rows_per_group=5)
    batches = f.scan(columns=["id"])
    assert all(b.schema.names == ["id"] for b in batches)


def test_columnar_file_filter_on_unprojected_column() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(20), rows_per_group=5)
    # Filter on score but project only id.
    batches = f.scan(predicate=col_lt("score", 5.0), columns=["id"])
    for b in batches:
        assert b.schema.names == ["id"]


def test_columnar_file_null_filter() -> None:
    schema = make_schema()
    cols = make_columns(20)
    expected_nulls = cols["score"].null_count
    f = ColumnarFile.from_columns(schema, cols, rows_per_group=5)
    got = sum(b.num_rows for b in f.scan(predicate=IsNull("score")))
    assert got == expected_nulls


def test_columnar_file_statistics() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(50), rows_per_group=10)
    stats = f.file_statistics()
    assert stats["id"].min_value == 0
    assert stats["id"].max_value == 49


def test_columnar_file_empty() -> None:
    schema = make_schema()
    empty_cols = {f.name: ColumnVector.empty(f.dtype) for f in schema.fields}
    f = ColumnarFile.from_columns(schema, empty_cols)
    assert f.num_rows == 0
    blob = f.serialize()
    assert ColumnarFile.deserialize(blob).num_rows == 0


def test_columnar_file_eq_filter_dictionary_column() -> None:
    schema = make_schema()
    f = ColumnarFile.from_columns(schema, make_columns(40), rows_per_group=10)
    rows = [r for b in f.scan(predicate=col_eq("name", "u1")) for r in b.rows()]
    assert all(r["name"] == "u1" for r in rows)
    assert len(rows) == 10  # every 4th row
