"""A record batch — a schema plus aligned column vectors.

:class:`RecordBatch` is the value the query engine produces and consumes between
operators: a :class:`~app.lakehouse.warehouse.types.Schema` and one
:class:`~app.lakehouse.warehouse.types.ColumnVector` per field, all the same
length. It is the materialised, in-memory analog of a row group, plus the helpers
operators need (project, filter by mask, take, slice, concat, row iteration).

Everything returns a *new* batch; batches are treated as immutable so a physical
plan is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.types import ColumnVector, Field, Schema


@dataclass(frozen=True, slots=True)
class RecordBatch:
    """A schema plus aligned column vectors."""

    schema: Schema
    columns: tuple[ColumnVector, ...]

    def __post_init__(self) -> None:
        if len(self.columns) != len(self.schema.fields):
            raise ValueError("one column required per field")
        lengths = {len(c) for c in self.columns}
        if len(lengths) > 1:
            raise ValueError(f"ragged batch: column lengths {sorted(lengths)}")
        for col, fld in zip(self.columns, self.schema.fields, strict=True):
            if col.dtype is not fld.dtype:
                raise TypeError(f"column {fld.name} dtype {col.dtype} != {fld.dtype}")

    @classmethod
    def from_mapping(cls, schema: Schema, columns: dict[str, ColumnVector]) -> RecordBatch:
        return cls(schema, tuple(columns[f.name] for f in schema.fields))

    @classmethod
    def empty(cls, schema: Schema) -> RecordBatch:
        return cls(schema, tuple(ColumnVector.empty(f.dtype) for f in schema.fields))

    @property
    def num_rows(self) -> int:
        return len(self.columns[0]) if self.columns else 0

    @property
    def num_columns(self) -> int:
        return len(self.columns)

    def column(self, name: str) -> ColumnVector:
        return self.columns[self.schema.index_of(name)]

    def mapping(self) -> dict[str, ColumnVector]:
        return {f.name: c for f, c in zip(self.schema.fields, self.columns, strict=True)}

    def project(self, names: list[str]) -> RecordBatch:
        """A batch with only ``names`` (in the requested order)."""
        cols = tuple(self.column(n) for n in names)
        return RecordBatch(self.schema.select(names), cols)

    def filter_mask(self, mask: list[bool]) -> RecordBatch:
        return RecordBatch(self.schema, tuple(c.filter_mask(mask) for c in self.columns))

    def take(self, indices: list[int]) -> RecordBatch:
        return RecordBatch(self.schema, tuple(c.take(indices) for c in self.columns))

    def slice(self, start: int, length: int) -> RecordBatch:
        idx = list(range(start, min(start + length, self.num_rows)))
        return self.take(idx)

    def with_column(self, fld: Field, vec: ColumnVector) -> RecordBatch:
        """A new batch with one column appended (or replaced if the name exists)."""
        if self.schema.has(fld.name):
            i = self.schema.index_of(fld.name)
            cols = list(self.columns)
            cols[i] = vec
            return RecordBatch(self.schema, tuple(cols))
        return RecordBatch(self.schema.with_fields([fld]), self.columns + (vec,))

    def rows(self) -> list[dict[str, Any]]:
        """Materialise as a list of row dicts (logical values, ``None`` for null)."""
        names = self.schema.names
        out: list[dict[str, Any]] = []
        for i in range(self.num_rows):
            out.append({name: self.column(name).get(i) for name in names})
        return out

    @classmethod
    def concat(cls, batches: list[RecordBatch]) -> RecordBatch:
        if not batches:
            raise ValueError("cannot concat zero batches")
        schema = batches[0].schema
        if any(b.schema != schema for b in batches):
            raise ValueError("cannot concat batches of differing schemas")
        cols: list[ColumnVector] = []
        for i in range(len(schema.fields)):
            acc = batches[0].columns[i]
            for b in batches[1:]:
                acc = acc.append(b.columns[i])
            cols.append(acc)
        return cls(schema, tuple(cols))


__all__ = ["RecordBatch"]
