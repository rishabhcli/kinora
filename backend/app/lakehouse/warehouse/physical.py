"""The physical plan — vectorized, pull-based operators.

Each :class:`PhysicalOperator` is an iterator of
:class:`~app.lakehouse.warehouse.batch.RecordBatch` es (Volcano-style ``execute``
that yields batches). Operators are vectorized: they transform whole columns at a
time via the kernels in :mod:`expr` and :mod:`aggregate`, never row-by-row Python
dispatch in the hot path (except the unavoidable group-key hashing and join probe).

Operators:

* :class:`TableScanExec` — pulls batches from a :class:`~contracts.Table` with
  pushdown + projection + snapshot already resolved.
* :class:`InMemoryScanExec` — scans pre-materialised batches (for tests / sibling
  facets that hand the engine data directly).
* :class:`FilterExec` — evaluate a boolean expr → keep-mask → filtered batch.
* :class:`ProjectExec` — evaluate named output expressions into a new batch.
* :class:`HashAggregateExec` — group-by + aggregates via a hash table of
  accumulators; empty grouping ⇒ a single global-aggregate row.
* :class:`HashJoinExec` — build a hash table on the right, probe with the left
  (inner / left outer), with right-side column renaming on name clashes.
* :class:`SortExec` — fully-materialising stable sort (NULLs last).
* :class:`LimitExec` — offset + limit with early stop.

The engine drives the root operator and concatenates its batches.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.lakehouse.warehouse.aggregate import AggregateSpec
from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.expr import Expr
from app.lakehouse.warehouse.logical import JoinType
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema, placeholder_for


class PhysicalOperator(ABC):
    """A pull-based, batch-producing operator."""

    @abstractmethod
    def schema(self) -> Schema: ...

    @abstractmethod
    def execute(self) -> Iterator[RecordBatch]: ...


@dataclass(slots=True)
class InMemoryScanExec(PhysicalOperator):
    out_schema: Schema
    batches: list[RecordBatch]

    def schema(self) -> Schema:
        return self.out_schema

    def execute(self) -> Iterator[RecordBatch]:
        yield from self.batches


@dataclass(slots=True)
class TableScanExec(PhysicalOperator):
    """Wraps a ``Table.scan`` (pushdown/projection/snapshot already resolved)."""

    out_schema: Schema
    batches: list[RecordBatch]

    def schema(self) -> Schema:
        return self.out_schema

    def execute(self) -> Iterator[RecordBatch]:
        yield from self.batches


@dataclass(slots=True)
class FilterExec(PhysicalOperator):
    child: PhysicalOperator
    predicate: Expr

    def schema(self) -> Schema:
        return self.child.schema()

    def execute(self) -> Iterator[RecordBatch]:
        for batch in self.child.execute():
            mask_vec = self.predicate.evaluate(batch)
            # NULL/unknown does not pass a filter.
            mask = [mask_vec.is_valid(i) and bool(mask_vec.value(i)) for i in range(len(mask_vec))]
            out = batch.filter_mask(mask)
            if out.num_rows:
                yield out


@dataclass(slots=True)
class ProjectExec(PhysicalOperator):
    child: PhysicalOperator
    expressions: tuple[tuple[str, Expr], ...]
    out_schema: Schema

    def schema(self) -> Schema:
        return self.out_schema

    def execute(self) -> Iterator[RecordBatch]:
        for batch in self.child.execute():
            cols = tuple(expr.evaluate(batch) for _name, expr in self.expressions)
            yield RecordBatch(self.out_schema, cols)


@dataclass(slots=True)
class HashAggregateExec(PhysicalOperator):
    child: PhysicalOperator
    group_by: tuple[str, ...]
    aggregates: tuple[AggregateSpec, ...]
    out_schema: Schema

    def schema(self) -> Schema:
        return self.out_schema

    def execute(self) -> Iterator[RecordBatch]:
        # group key -> (key_values, [accumulators]); ordered by first appearance.
        groups: dict[tuple[Any, ...], tuple[tuple[Any, ...], list[Any]]] = {}
        order: list[tuple[Any, ...]] = []
        agg_inputs = [
            agg.input.evaluate if agg.input is not None else None for agg in self.aggregates
        ]
        for batch in self.child.execute():
            key_vecs = [batch.column(g) for g in self.group_by]
            # Pre-evaluate each aggregate's input vector for this batch.
            input_vecs = [
                ev(batch) if ev is not None else None for ev in agg_inputs
            ]
            for i in range(batch.num_rows):
                key = tuple(v.get(i) for v in key_vecs)
                if key not in groups:
                    accs = [a.new_accumulator() for a in self.aggregates]
                    groups[key] = (key, accs)
                    order.append(key)
                accs = groups[key][1]
                for j, vec in enumerate(input_vecs):
                    if vec is None:  # COUNT(*)
                        accs[j].update(None, True)
                    else:
                        accs[j].update(vec.value(i), vec.is_valid(i))

        # Build the output batch.
        if not order and not self.group_by:
            # Global aggregate over zero rows: emit one row of empty accumulators.
            accs = [a.new_accumulator() for a in self.aggregates]
            order = [()]
            groups[()] = ((), accs)

        out_cols: list[list[Any]] = [[] for _ in self.out_schema.fields]
        out_valid: list[list[bool]] = [[] for _ in self.out_schema.fields]
        for key in order:
            key_vals, accs = groups[key]
            ci = 0
            for gi, _g in enumerate(self.group_by):
                val = key_vals[gi]
                gph = placeholder_for(self.out_schema.fields[ci].dtype)
                out_cols[ci].append(val if val is not None else gph)
                out_valid[ci].append(val is not None)
                ci += 1
            for acc, spec in zip(accs, self.aggregates, strict=True):
                val, ok = acc.result()
                dtype = self.out_schema.field(spec.output_name).dtype
                out_cols[ci].append(val if ok else placeholder_for(dtype))
                out_valid[ci].append(ok)
                ci += 1

        columns = tuple(
            ColumnVector(f.dtype, out_cols[i], out_valid[i])
            for i, f in enumerate(self.out_schema.fields)
        )
        yield RecordBatch(self.out_schema, columns)


@dataclass(slots=True)
class HashJoinExec(PhysicalOperator):
    left: PhysicalOperator
    right: PhysicalOperator
    on: tuple[tuple[str, str], ...]
    how: JoinType
    out_schema: Schema
    right_prefix: str

    def schema(self) -> Schema:
        return self.out_schema

    def execute(self) -> Iterator[RecordBatch]:
        right_batch = _materialise(self.right)
        left_keys = [lk for lk, _rk in self.on]
        right_keys = [rk for _lk, rk in self.on]

        # Build side: key tuple -> list of right row indices.
        index: dict[tuple[Any, ...], list[int]] = {}
        rk_vecs = [right_batch.column(rk) for rk in right_keys]
        for i in range(right_batch.num_rows):
            key = tuple(v.get(i) for v in rk_vecs)
            if any(k is None for k in key):
                continue  # NULL keys never match (SQL)
            index.setdefault(key, []).append(i)

        for lbatch in self.left.execute():
            lk_vecs = [lbatch.column(lk) for lk in left_keys]
            left_take: list[int] = []
            right_take: list[int | None] = []
            for i in range(lbatch.num_rows):
                key = tuple(v.get(i) for v in lk_vecs)
                matches = [] if any(k is None for k in key) else index.get(key, [])
                if matches:
                    for rj in matches:
                        left_take.append(i)
                        right_take.append(rj)
                elif self.how is JoinType.LEFT:
                    left_take.append(i)
                    right_take.append(None)
            if not left_take:
                continue
            yield self._assemble(lbatch, right_batch, left_take, right_take)

    def _assemble(
        self,
        lbatch: RecordBatch,
        rbatch: RecordBatch,
        left_take: list[int],
        right_take: list[int | None],
    ) -> RecordBatch:
        # Left columns: simple gather. Right columns: gather with NULLs for
        # unmatched (LEFT join) rows. Output naming is already resolved in
        # ``out_schema`` (right-side clashes prefixed), so we position by index.
        cols: list[ColumnVector] = [c.take(left_take) for c in lbatch.columns]
        for src in rbatch.columns:
            dtype = src.dtype
            ph = placeholder_for(dtype)
            values: list[Any] = []
            valid: list[bool] = []
            for rj in right_take:
                if rj is None:
                    values.append(ph)
                    valid.append(False)
                else:
                    values.append(src.value(rj))
                    valid.append(src.is_valid(rj))
            cols.append(ColumnVector(dtype, values, valid))
        return RecordBatch(self.out_schema, tuple(cols))


@dataclass(slots=True)
class SortExec(PhysicalOperator):
    child: PhysicalOperator
    keys: tuple[tuple[str, bool], ...]

    def schema(self) -> Schema:
        return self.child.schema()

    def execute(self) -> Iterator[RecordBatch]:
        full = _materialise(self.child)
        n = full.num_rows
        key_vecs = [(full.column(name), desc) for name, desc in self.keys]
        indices = list(range(n))

        # Stable multi-key sort applied from the least-significant key upward.
        # NULLs sort last in *both* directions: with ``reverse`` the per-cell null
        # rank is negated so the sentinel stays at the tail after reversal.
        for vec, desc in reversed(key_vecs):

            def key_fn(i: int, v: ColumnVector = vec, d: bool = desc) -> tuple[int, Any]:
                return _sort_cell(v, i, d)

            indices.sort(key=key_fn, reverse=desc)
        yield full.take(indices)


@dataclass(slots=True)
class LimitExec(PhysicalOperator):
    child: PhysicalOperator
    count: int
    offset: int

    def schema(self) -> Schema:
        return self.child.schema()

    def execute(self) -> Iterator[RecordBatch]:
        skipped = 0
        emitted = 0
        for batch in self.child.execute():
            if emitted >= self.count:
                return
            start = 0
            if skipped < self.offset:
                take = min(self.offset - skipped, batch.num_rows)
                skipped += take
                start = take
                if start >= batch.num_rows:
                    continue
            remaining = self.count - emitted
            avail = batch.num_rows - start
            length = min(remaining, avail)
            out = batch.slice(start, length)
            emitted += out.num_rows
            if out.num_rows:
                yield out


# --------------------------------------------------------------------------- #


def _materialise(op: PhysicalOperator) -> RecordBatch:
    batches = list(op.execute())
    if not batches:
        return RecordBatch.empty(op.schema())
    return RecordBatch.concat(batches)


def _types(schema: Schema) -> dict[str, LogicalType]:
    return {f.name: f.dtype for f in schema.fields}


def _sort_cell(vec: ColumnVector, i: int, descending: bool) -> tuple[int, Any]:
    """A sort key that keeps NULLs at the tail under both sort directions.

    The leading ``rank`` is ``0`` for present values and ``1`` for NULLs on an
    ascending sort. On a descending sort the whole sort is reversed, so we flip the
    rank to ``-1`` so NULLs land *after* reversal too. Present rows always carry
    ``rank=0`` so they compare on value within their key group.
    """
    if not vec.is_valid(i):
        return (-1 if descending else 1, placeholder_for(vec.dtype))
    return (0, vec.value(i))


def make_field(name: str, dtype: LogicalType) -> Field:
    return Field(name=name, dtype=dtype)


__all__ = [
    "FilterExec",
    "HashAggregateExec",
    "HashJoinExec",
    "InMemoryScanExec",
    "LimitExec",
    "PhysicalOperator",
    "ProjectExec",
    "SortExec",
    "TableScanExec",
]
