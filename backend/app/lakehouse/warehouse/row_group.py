"""A row group — a horizontal slice of a table as aligned column chunks.

A :class:`RowGroup` holds one :class:`~app.lakehouse.warehouse.column_chunk.ColumnChunk`
per schema field, all the same length (``num_rows``). It is the unit the columnar
file partitions data into and the unit predicate pushdown skips: if a predicate
proves (from the group's per-column statistics) that no row can match, the whole
group is skipped without decoding.

Reading a group yields a *batch* — a ``dict[name -> ColumnVector]`` — which is what
the vectorized query operators consume. A group can also be read with a column
projection (decode only the requested columns).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.lakehouse.warehouse.column_chunk import ColumnChunk
from app.lakehouse.warehouse.predicate import Predicate
from app.lakehouse.warehouse.statistics import ColumnStatistics
from app.lakehouse.warehouse.types import ColumnVector, Schema


@dataclass(frozen=True, slots=True)
class RowGroup:
    """Aligned column chunks for one horizontal slice of a table."""

    schema: Schema
    num_rows: int
    chunks: tuple[ColumnChunk, ...]

    def __post_init__(self) -> None:
        if len(self.chunks) != len(self.schema.fields):
            raise ValueError("one chunk required per schema field")
        for chunk, fld in zip(self.chunks, self.schema.fields, strict=True):
            if chunk.name != fld.name:
                raise ValueError(f"chunk/field name mismatch: {chunk.name} != {fld.name}")
            if chunk.num_rows != self.num_rows:
                raise ValueError(
                    f"chunk {chunk.name} has {chunk.num_rows} rows, expected {self.num_rows}"
                )

    @classmethod
    def write(
        cls,
        schema: Schema,
        columns: dict[str, ColumnVector],
        *,
        zone_size: int | None = None,
    ) -> RowGroup:
        """Encode a batch (one vector per field) into a row group."""
        lengths = {len(v) for v in columns.values()}
        if len(lengths) > 1:
            raise ValueError(f"ragged columns: lengths {sorted(lengths)}")
        num_rows = next(iter(lengths)) if lengths else 0
        chunks: list[ColumnChunk] = []
        for fld in schema.fields:
            if fld.name not in columns:
                raise KeyError(f"missing column {fld.name}")
            vec = columns[fld.name]
            if vec.dtype is not fld.dtype:
                raise TypeError(
                    f"column {fld.name} dtype {vec.dtype} != schema {fld.dtype}"
                )
            chunks.append(ColumnChunk.write(fld.name, vec, zone_size=zone_size))
        return cls(schema=schema, num_rows=num_rows, chunks=tuple(chunks))

    def column_statistics(self) -> dict[str, ColumnStatistics]:
        """Per-column statistics keyed by name (drives pushdown)."""
        return {c.name: c.statistics for c in self.chunks}

    def chunk(self, name: str) -> ColumnChunk:
        for c in self.chunks:
            if c.name == name:
                return c
        raise KeyError(name)

    def read(self, columns: list[str] | None = None) -> dict[str, ColumnVector]:
        """Decode the group into a batch, optionally projecting columns."""
        names = columns if columns is not None else self.schema.names
        return {name: self.chunk(name).read() for name in names}

    def can_skip(self, predicate: Predicate) -> bool:
        """Whether ``predicate`` provably matches no row in this group."""
        return predicate.can_skip_statistics(self.column_statistics())


__all__ = ["RowGroup"]
