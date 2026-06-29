"""The contracts the sibling lakehouse facets (feature store, semantic layer) consume.

This module is the **stable seam** between facet A (this warehouse) and the other
facets. It deliberately depends only on the storage/value types, never on the
catalog or engine implementations, so a sibling can type-check against it without
importing the world.

* :class:`Table` — a named, versioned dataset: a schema, a partition spec, and the
  ability to ``scan`` (with optional predicate pushdown + projection, optionally as
  of a snapshot) into :class:`~app.lakehouse.warehouse.batch.RecordBatch` es. The
  catalog's ``CatalogTable`` implements this.
* :class:`QueryEngine` — runs a logical query (a plan or SQL-shaped request) over
  one or more tables and returns a :class:`RecordBatch`. The vectorized engine
  implements this.
* :class:`TableScan` — the resolved scan description a planner hands to the engine.

These are :class:`typing.Protocol` s (structural) so an implementation need not
inherit them; it only needs the matching shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.partition import PartitionSpec
from app.lakehouse.warehouse.predicate import Predicate
from app.lakehouse.warehouse.types import Schema


@dataclass(frozen=True, slots=True)
class TableScan:
    """A resolved request to read a table.

    ``snapshot_id=None`` reads the current snapshot; a value reads that snapshot
    (time-travel). ``columns=None`` reads every column.
    """

    table_name: str
    columns: list[str] | None = None
    predicate: Predicate | None = None
    snapshot_id: int | None = None


@runtime_checkable
class Table(Protocol):
    """A named, versioned, scannable dataset (the contract sibling facets consume)."""

    @property
    def name(self) -> str: ...

    @property
    def schema(self) -> Schema: ...

    @property
    def partition_spec(self) -> PartitionSpec: ...

    def scan(
        self,
        *,
        columns: list[str] | None = None,
        predicate: Predicate | None = None,
        snapshot_id: int | None = None,
    ) -> list[RecordBatch]:
        """Read matching row groups as batches, applying pushdown + the filter."""
        ...

    def current_snapshot_id(self) -> int | None:
        """The id of the current snapshot, or ``None`` for an empty table."""
        ...


@runtime_checkable
class QueryEngine(Protocol):
    """Runs a query over a set of tables and returns a single batch."""

    def execute(self, plan: object) -> RecordBatch:
        """Run a (logical) plan to completion, returning the result batch."""
        ...


__all__ = ["QueryEngine", "Table", "TableScan"]
