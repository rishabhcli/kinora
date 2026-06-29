"""The columnar file format: row groups + a self-describing footer.

A :class:`ColumnarFile` is a list of row groups sharing one schema, plus a footer
that records, per group, the per-column statistics. Predicate pushdown happens at
read time: :meth:`ColumnarFile.scan` consults the footer and skips any row group
whose statistics prove no row can match the predicate — so a filter on a clustered
column reads only the relevant groups.

On-disk layout (deterministic; stdlib only)::

    MAGIC (4 bytes)  "KLWF"  -- Kinora Lakehouse Warehouse File
    [ row group 0 bytes ]
    [ row group 1 bytes ]
    ...
    FOOTER bytes      -- schema + per-group offsets/lengths/row-counts
    uint32 footer_len (little-endian)
    MAGIC (4 bytes)   trailer

The trailing magic + footer length let a reader seek the footer from the file end
(the Parquet pattern). We keep the whole thing as ``bytes`` here — object storage
puts the blob in OSS/MinIO; the catalog tracks file paths.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.column_chunk import ColumnChunk
from app.lakehouse.warehouse.encoding import read_uvarint, write_uvarint
from app.lakehouse.warehouse.predicate import Predicate
from app.lakehouse.warehouse.row_group import RowGroup
from app.lakehouse.warehouse.statistics import ColumnStatistics
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema

MAGIC = b"KLWF"


@dataclass(frozen=True, slots=True)
class ColumnarFile:
    """An immutable columnar file of row groups under one schema."""

    schema: Schema
    row_groups: tuple[RowGroup, ...]

    @classmethod
    def from_batches(
        cls,
        schema: Schema,
        batches: list[RecordBatch],
        *,
        zone_size: int | None = None,
    ) -> ColumnarFile:
        """Build a file from in-memory batches (one row group per batch)."""
        groups: list[RowGroup] = []
        for batch in batches:
            if batch.schema != schema:
                raise ValueError("batch schema does not match file schema")
            groups.append(RowGroup.write(schema, batch.mapping(), zone_size=zone_size))
        return cls(schema=schema, row_groups=tuple(groups))

    @classmethod
    def from_columns(
        cls,
        schema: Schema,
        columns: dict[str, ColumnVector],
        *,
        rows_per_group: int = 4096,
        zone_size: int | None = None,
    ) -> ColumnarFile:
        """Build a file from full column vectors, sliced into row groups."""
        total = len(next(iter(columns.values()))) if columns else 0
        batches: list[RecordBatch] = []
        for start in range(0, total, rows_per_group):
            end = min(start + rows_per_group, total)
            idx = list(range(start, end))
            sliced = {name: vec.take(idx) for name, vec in columns.items()}
            batches.append(RecordBatch.from_mapping(schema, sliced))
        if not batches:
            batches = [RecordBatch.empty(schema)]
        return cls.from_batches(schema, batches, zone_size=zone_size)

    @property
    def num_rows(self) -> int:
        return sum(g.num_rows for g in self.row_groups)

    @property
    def num_row_groups(self) -> int:
        return len(self.row_groups)

    def file_statistics(self) -> dict[str, ColumnStatistics]:
        """File-level merged statistics per column."""
        from app.lakehouse.warehouse.statistics import merge_statistics

        out: dict[str, ColumnStatistics] = {}
        for fld in self.schema.fields:
            per_group = [g.column_statistics()[fld.name] for g in self.row_groups]
            out[fld.name] = merge_statistics(per_group)
        return out

    def scan(
        self,
        *,
        predicate: Predicate | None = None,
        columns: list[str] | None = None,
    ) -> list[RecordBatch]:
        """Read matching row groups as batches, applying pushdown + the filter.

        Row groups proven empty by ``predicate``'s statistics are skipped. The
        predicate is then applied row-wise to the surviving groups. ``columns``
        projects the output (the predicate may reference columns not projected;
        they are decoded for filtering then dropped).
        """
        out: list[RecordBatch] = []
        proj = columns if columns is not None else self.schema.names
        pred_cols = predicate.columns() if predicate is not None else set()
        decode_cols = list(dict.fromkeys([*proj, *sorted(pred_cols)]))
        for group in self.row_groups:
            if predicate is not None and group.can_skip(predicate):
                continue
            decoded = group.read(decode_cols)
            if predicate is not None:
                mask = predicate.evaluate(decoded)
                decoded = {name: vec.filter_mask(mask) for name, vec in decoded.items()}
            out_schema = self.schema.select(proj)
            out.append(RecordBatch.from_mapping(out_schema, decoded))
        return out

    def read_all(self, *, columns: list[str] | None = None) -> RecordBatch:
        """Decode the entire file into a single batch."""
        batches = self.scan(columns=columns)
        proj = columns if columns is not None else self.schema.names
        if not batches:
            return RecordBatch.empty(self.schema.select(proj))
        return RecordBatch.concat(batches)

    # -- serialisation --------------------------------------------------------

    def serialize(self) -> bytes:
        out = bytearray()
        out.extend(MAGIC)
        group_offsets: list[tuple[int, int, int]] = []  # (offset, length, num_rows)
        for group in self.row_groups:
            offset = len(out)
            payload = bytearray()
            write_uvarint(payload, group.num_rows)
            write_uvarint(payload, len(group.chunks))
            for chunk in group.chunks:
                chunk_bytes = chunk.serialize()
                write_uvarint(payload, len(chunk_bytes))
                payload.extend(chunk_bytes)
            out.extend(payload)
            group_offsets.append((offset, len(payload), group.num_rows))

        footer = bytearray()
        _write_schema(footer, self.schema)
        write_uvarint(footer, len(group_offsets))
        for offset, length, num_rows in group_offsets:
            write_uvarint(footer, offset)
            write_uvarint(footer, length)
            write_uvarint(footer, num_rows)
        footer_start = len(out)
        out.extend(footer)
        out.extend(struct.pack("<I", len(out) - footer_start))
        out.extend(MAGIC)
        return bytes(out)

    @classmethod
    def deserialize(cls, buf: bytes) -> ColumnarFile:
        if buf[:4] != MAGIC or buf[-4:] != MAGIC:
            raise ValueError("not a KLWF columnar file (bad magic)")
        footer_len = struct.unpack_from("<I", buf, len(buf) - 8)[0]
        footer_start = len(buf) - 8 - footer_len
        pos = footer_start
        schema, pos = _read_schema(buf, pos)
        n_groups, pos = read_uvarint(buf, pos)
        groups: list[RowGroup] = []
        for _ in range(n_groups):
            offset, pos = read_uvarint(buf, pos)
            _length, pos = read_uvarint(buf, pos)
            _num_rows, pos = read_uvarint(buf, pos)
            groups.append(_read_group(buf, offset, schema))
        return cls(schema=schema, row_groups=tuple(groups))


def _read_group(buf: bytes, offset: int, schema: Schema) -> RowGroup:
    pos = offset
    num_rows, pos = read_uvarint(buf, pos)
    n_chunks, pos = read_uvarint(buf, pos)
    chunks: list[ColumnChunk] = []
    for _ in range(n_chunks):
        clen, pos = read_uvarint(buf, pos)
        chunks.append(ColumnChunk.deserialize(bytes(buf[pos : pos + clen])))
        pos += clen
    return RowGroup(schema=schema, num_rows=num_rows, chunks=tuple(chunks))


def _write_schema(out: bytearray, schema: Schema) -> None:
    write_uvarint(out, len(schema.fields))
    for fld in schema.fields:
        name = fld.name.encode("utf-8")
        write_uvarint(out, len(name))
        out.extend(name)
        dt = fld.dtype.value.encode("utf-8")
        write_uvarint(out, len(dt))
        out.extend(dt)
        out.append(1 if fld.nullable else 0)
        write_uvarint(out, fld.scale)


def _read_schema(buf: bytes, pos: int) -> tuple[Schema, int]:
    n, pos = read_uvarint(buf, pos)
    fields: list[Field] = []
    for _ in range(n):
        nlen, pos = read_uvarint(buf, pos)
        name = buf[pos : pos + nlen].decode("utf-8")
        pos += nlen
        dlen, pos = read_uvarint(buf, pos)
        dtype = LogicalType(buf[pos : pos + dlen].decode("utf-8"))
        pos += dlen
        nullable = bool(buf[pos])
        pos += 1
        scale, pos = read_uvarint(buf, pos)
        fields.append(Field(name=name, dtype=dtype, nullable=nullable, scale=scale))
    return Schema(tuple(fields)), pos


__all__ = ["MAGIC", "ColumnarFile"]
