"""The logical query plan — *what* to compute, not *how*.

A :class:`LogicalPlan` is an immutable tree of relational nodes. Each node can
report its output :class:`~app.lakehouse.warehouse.types.Schema` given its
children, which the planner uses to type-check and which the physical operators
inherit. Logical plans are built with a small fluent builder
(:class:`PlanBuilder`) so tests and the engine read like a query.

Nodes:

* :class:`Scan` — a base table read with optional pushed-down predicate +
  projection + snapshot (time-travel).
* :class:`Filter` — keep rows where a boolean expression holds.
* :class:`Project` — compute a list of named output expressions.
* :class:`Aggregate` — group-by + aggregates (empty grouping ⇒ a global aggregate).
* :class:`Join` — equi-join two inputs on key column pairs (inner/left).
* :class:`Sort` — order by a list of ``(column, descending)`` keys.
* :class:`Limit` — first ``n`` rows (after ``offset``).

The plan references tables by name; the engine resolves names against a catalog.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.lakehouse.warehouse.aggregate import AggregateSpec
from app.lakehouse.warehouse.expr import Expr
from app.lakehouse.warehouse.predicate import Predicate
from app.lakehouse.warehouse.types import Field, LogicalType, Schema


class LogicalPlan(ABC):
    """Base of the logical plan tree."""

    @abstractmethod
    def output_schema(self) -> Schema: ...

    @abstractmethod
    def children(self) -> list[LogicalPlan]: ...

    # -- fluent combinators ---------------------------------------------------

    def filter(self, predicate: Expr) -> Filter:
        return Filter(self, predicate)

    def project(self, exprs: list[tuple[str, Expr]]) -> Project:
        return Project(self, tuple(exprs))

    def aggregate(self, group_by: list[str], aggregates: list[AggregateSpec]) -> Aggregate:
        return Aggregate(self, tuple(group_by), tuple(aggregates))

    def sort(self, keys: list[tuple[str, bool]]) -> Sort:
        return Sort(self, tuple(keys))

    def limit(self, n: int, offset: int = 0) -> Limit:
        return Limit(self, n, offset)

    def join(
        self,
        right: LogicalPlan,
        on: list[tuple[str, str]],
        *,
        how: JoinType = None,  # type: ignore[assignment]
    ) -> Join:
        return Join(self, right, tuple(on), how or JoinType.INNER)


@dataclass(frozen=True, slots=True)
class Scan(LogicalPlan):
    table_name: str
    table_schema: Schema
    projection: tuple[str, ...] | None = None
    predicate: Predicate | None = None
    snapshot_id: int | None = None

    def output_schema(self) -> Schema:
        if self.projection is None:
            return self.table_schema
        return self.table_schema.select(list(self.projection))

    def children(self) -> list[LogicalPlan]:
        return []


@dataclass(frozen=True, slots=True)
class Filter(LogicalPlan):
    input: LogicalPlan
    predicate: Expr

    def output_schema(self) -> Schema:
        return self.input.output_schema()

    def children(self) -> list[LogicalPlan]:
        return [self.input]


@dataclass(frozen=True, slots=True)
class Project(LogicalPlan):
    input: LogicalPlan
    expressions: tuple[tuple[str, Expr], ...]

    def output_schema(self) -> Schema:
        types = _schema_types(self.input.output_schema())
        fields = [
            Field(name=name, dtype=expr.result_type(types))
            for name, expr in self.expressions
        ]
        return Schema(tuple(fields))

    def children(self) -> list[LogicalPlan]:
        return [self.input]


@dataclass(frozen=True, slots=True)
class Aggregate(LogicalPlan):
    input: LogicalPlan
    group_by: tuple[str, ...]
    aggregates: tuple[AggregateSpec, ...]

    def output_schema(self) -> Schema:
        in_schema = self.input.output_schema()
        types = _schema_types(in_schema)
        fields: list[Field] = [in_schema.field(g) for g in self.group_by]
        for agg in self.aggregates:
            fields.append(Field(name=agg.output_name, dtype=agg.result_type(types)))
        return Schema(tuple(fields))

    def children(self) -> list[LogicalPlan]:
        return [self.input]


class JoinType(enum.StrEnum):
    INNER = "inner"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class Join(LogicalPlan):
    left: LogicalPlan
    right: LogicalPlan
    on: tuple[tuple[str, str], ...]
    how: JoinType = JoinType.INNER
    right_prefix: str = "right_"

    def output_schema(self) -> Schema:
        left_schema = self.left.output_schema()
        right_schema = self.right.output_schema()
        left_names = set(left_schema.names)
        fields = list(left_schema.fields)
        for f in right_schema.fields:
            name = f.name if f.name not in left_names else f"{self.right_prefix}{f.name}"
            # On a LEFT join, right columns become nullable.
            nullable = f.nullable or self.how is JoinType.LEFT
            fields.append(Field(name=name, dtype=f.dtype, nullable=nullable, scale=f.scale))
        return Schema(tuple(fields))

    def children(self) -> list[LogicalPlan]:
        return [self.left, self.right]


@dataclass(frozen=True, slots=True)
class Sort(LogicalPlan):
    input: LogicalPlan
    keys: tuple[tuple[str, bool], ...]  # (column, descending)

    def output_schema(self) -> Schema:
        return self.input.output_schema()

    def children(self) -> list[LogicalPlan]:
        return [self.input]


@dataclass(frozen=True, slots=True)
class Limit(LogicalPlan):
    input: LogicalPlan
    count: int
    offset: int = 0

    def output_schema(self) -> Schema:
        return self.input.output_schema()

    def children(self) -> list[LogicalPlan]:
        return [self.input]


def _schema_types(schema: Schema) -> dict[str, LogicalType]:
    return {f.name: f.dtype for f in schema.fields}


@dataclass(frozen=True, slots=True)
class PlanBuilder:
    """A fluent entry point: ``PlanBuilder.scan(...).filter(...).project(...)``."""

    plan: LogicalPlan = field(default=None)  # type: ignore[assignment]

    @classmethod
    def scan(
        cls,
        table_name: str,
        table_schema: Schema,
        *,
        columns: list[str] | None = None,
        predicate: Predicate | None = None,
        snapshot_id: int | None = None,
    ) -> Scan:
        return Scan(
            table_name=table_name,
            table_schema=table_schema,
            projection=tuple(columns) if columns is not None else None,
            predicate=predicate,
            snapshot_id=snapshot_id,
        )


__all__ = [
    "Aggregate",
    "Filter",
    "Join",
    "JoinType",
    "Limit",
    "LogicalPlan",
    "PlanBuilder",
    "Project",
    "Scan",
    "Sort",
]
