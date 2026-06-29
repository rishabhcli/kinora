"""The vectorized query engine — the ``QueryEngine`` contract over a catalog.

:class:`WarehouseQueryEngine` resolves ``Scan`` table names against a
:class:`~app.lakehouse.warehouse.contracts.Table` registry (typically a
:class:`~app.lakehouse.warehouse.catalog.Catalog`), pushes each scan's
predicate/projection/snapshot down to ``Table.scan`` (so columnar pruning happens
at the source), plans the logical tree into vectorized physical operators, runs it,
and concatenates the result into one :class:`~app.lakehouse.warehouse.batch.RecordBatch`.

It also exposes a small *query builder* surface (:meth:`scan`) so a caller can
write a query fluently without hand-constructing logical nodes, and a
:meth:`sql_like` convenience that maps a tiny declarative request onto the plan
nodes (no parser — a structured dict, for the semantic-layer facet to target).
"""

from __future__ import annotations

from typing import Any

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.contracts import Table
from app.lakehouse.warehouse.logical import LogicalPlan, Scan
from app.lakehouse.warehouse.planner import Planner, TableResolver
from app.lakehouse.warehouse.types import Schema


class TableNotRegisteredError(KeyError):
    """Raised when a plan references a table the engine cannot resolve."""


class _CatalogResolver(TableResolver):
    """Resolves ``Scan`` nodes against a name → ``Table`` mapping with pushdown."""

    def __init__(self, tables: dict[str, Table]) -> None:
        self._tables = tables

    def resolve_scan(self, scan: Scan) -> list[RecordBatch]:
        table = self._tables.get(scan.table_name)
        if table is None:
            raise TableNotRegisteredError(scan.table_name)
        return table.scan(
            columns=list(scan.projection) if scan.projection is not None else None,
            predicate=scan.predicate,
            snapshot_id=scan.snapshot_id,
        )


class WarehouseQueryEngine:
    """Plans and executes logical plans over registered tables (implements ``QueryEngine``)."""

    def __init__(self, tables: dict[str, Table] | None = None) -> None:
        self._tables: dict[str, Table] = dict(tables) if tables else {}

    @classmethod
    def from_catalog(cls, catalog: Any) -> WarehouseQueryEngine:
        """Build an engine over every table currently in ``catalog``."""
        tables = {name: catalog.table(name) for name in catalog.list_tables()}
        return cls(tables)

    def register(self, table: Table) -> None:
        self._tables[table.name] = table

    def table_schema(self, name: str) -> Schema:
        return self._tables[name].schema

    def scan(
        self,
        table_name: str,
        *,
        columns: list[str] | None = None,
    ) -> Scan:
        """Start a fluent logical plan from a registered table."""
        table = self._tables.get(table_name)
        if table is None:
            raise TableNotRegisteredError(table_name)
        return Scan(
            table_name=table_name,
            table_schema=table.schema,
            projection=tuple(columns) if columns is not None else None,
        )

    def execute(self, plan: object, *, optimize_plan: bool = True) -> RecordBatch:
        if not isinstance(plan, LogicalPlan):
            raise TypeError("plan must be a LogicalPlan")
        resolver = _CatalogResolver(self._resolve_all(plan))
        physical = Planner(resolver).plan(plan, optimize_plan=optimize_plan)
        batches = list(physical.execute())
        if not batches:
            return RecordBatch.empty(plan.output_schema())
        return RecordBatch.concat(batches)

    def _resolve_all(self, plan: LogicalPlan) -> dict[str, Table]:
        """Collect the tables every ``Scan`` in the plan needs."""
        needed: dict[str, Table] = {}
        self._collect_scans(plan, needed)
        return needed

    def _collect_scans(self, plan: LogicalPlan, acc: dict[str, Table]) -> None:
        if isinstance(plan, Scan):
            table = self._tables.get(plan.table_name)
            if table is None:
                raise TableNotRegisteredError(plan.table_name)
            acc[plan.table_name] = table
        for child in plan.children():
            self._collect_scans(child, acc)


__all__ = ["TableNotRegisteredError", "WarehouseQueryEngine"]
