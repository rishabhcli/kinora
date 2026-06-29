"""Core typed primitives for the semantic / metrics layer.

These are the shared vocabulary every other module speaks: data types, time
grains, aggregation kinds, comparison operators, and a small, *pure* filter
expression AST. Everything here is frozen, hashable, and side-effect-free so the
compiler can fold it into a stable plan hash (caching, lineage) without surprise.

The filter AST is deliberately minimal but complete: leaf predicates compare a
*field reference* against a literal (or a list, for ``IN``); composite nodes are
``AND`` / ``OR`` / ``NOT``. It is engine-agnostic — the SQL fallback renders it
to a ``WHERE`` clause, the in-memory engine evaluates it directly over rows, and
governance composes its row policies into the very same tree.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #

#: A valid semantic identifier: a snake_case-ish name (LookML/MetricFlow style).
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def validate_identifier(name: str, *, what: str = "identifier") -> str:
    """Return ``name`` if it is a valid lowercase semantic identifier, else raise.

    Identifiers name entities, dimensions, measures, metrics, and models. We pin
    them to ``[a-z_][a-z0-9_]*`` so they are safe to interpolate into SQL
    identifiers and to use as stable cache/lineage keys. The validation is the
    only injection guard the SQL fallback relies on for *names* (literals are
    always parameterised).
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(
            f"invalid {what} {name!r}: must match {_IDENT_RE.pattern} (lowercase snake_case)"
        )
    return name


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


class DataType(enum.StrEnum):
    """The column data types the semantic layer reasons about."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"
    DATE = "date"


#: A scalar literal accepted by a filter or measure default.
Scalar = str | int | float | bool | datetime | date | None


# --------------------------------------------------------------------------- #
# Time grains
# --------------------------------------------------------------------------- #


class TimeGrain(enum.StrEnum):
    """Time-dimension truncation grains, coarsest-last ordering via :data:`_GRAIN_ORDER`."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


#: Coarsening order; used to validate that a query grain is >= a metric's base
#: grain and to drive cumulative windows / time-comparison offsets.
_GRAIN_ORDER: tuple[TimeGrain, ...] = (
    TimeGrain.HOUR,
    TimeGrain.DAY,
    TimeGrain.WEEK,
    TimeGrain.MONTH,
    TimeGrain.QUARTER,
    TimeGrain.YEAR,
)


def grain_rank(grain: TimeGrain) -> int:
    """Return the coarsening rank of ``grain`` (HOUR=0 … YEAR=5)."""
    return _GRAIN_ORDER.index(grain)


def is_coarser_or_equal(grain: TimeGrain, base: TimeGrain) -> bool:
    """True if ``grain`` is at least as coarse as ``base`` (can roll up from base)."""
    return grain_rank(grain) >= grain_rank(base)


# --------------------------------------------------------------------------- #
# Aggregations
# --------------------------------------------------------------------------- #


class Aggregation(enum.StrEnum):
    """The aggregation a *measure* applies to its underlying expression."""

    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MIN = "min"
    MAX = "max"
    AVERAGE = "average"
    #: A measure that is already aggregated upstream (used verbatim, no re-agg).
    #: Compose it with care — semantically additive only across the grain it was
    #: pre-aggregated at.
    SUM_BOOLEAN = "sum_boolean"


#: Aggregations that are *additive* across dimensions (safe to roll up further by
#: re-summing partial aggregates — the property the materialization advisor and
#: cumulative metrics rely on). ``AVERAGE`` / ``COUNT_DISTINCT`` are non-additive.
ADDITIVE_AGGREGATIONS: frozenset[Aggregation] = frozenset(
    {Aggregation.SUM, Aggregation.COUNT, Aggregation.SUM_BOOLEAN}
)


def is_additive(agg: Aggregation) -> bool:
    """True if partial aggregates of ``agg`` can be summed to a coarser grain."""
    return agg in ADDITIVE_AGGREGATIONS


# --------------------------------------------------------------------------- #
# Comparison operators (filter leaves)
# --------------------------------------------------------------------------- #


class Comparison(enum.StrEnum):
    """Leaf comparison operators for filter predicates."""

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


_COMPARISON_SQL: Mapping[Comparison, str] = {
    Comparison.EQ: "=",
    Comparison.NEQ: "<>",
    Comparison.GT: ">",
    Comparison.GTE: ">=",
    Comparison.LT: "<",
    Comparison.LTE: "<=",
    Comparison.IN: "IN",
    Comparison.NOT_IN: "NOT IN",
    Comparison.IS_NULL: "IS NULL",
    Comparison.IS_NOT_NULL: "IS NOT NULL",
}


def comparison_sql(op: Comparison) -> str:
    """The SQL operator token for a comparison (used by the SQL fallback)."""
    return _COMPARISON_SQL[op]


# --------------------------------------------------------------------------- #
# Field references
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A reference to a dimension or column by name, optionally entity-qualified.

    ``entity`` is the semantic-model name; ``name`` is the dimension/column. The
    unqualified form (``entity is None``) resolves against the query's primary
    model. The :pyattr:`qualified` string is the stable key used in plan hashes.
    """

    name: str
    entity: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.name, what="field name")
        if self.entity is not None:
            validate_identifier(self.entity, what="entity name")

    @property
    def qualified(self) -> str:
        return f"{self.entity}.{self.name}" if self.entity else self.name

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.qualified


def parse_field_ref(spec: str) -> FieldRef:
    """Parse ``"entity.field"`` or ``"field"`` into a :class:`FieldRef`."""
    if "." in spec:
        entity, _, name = spec.partition(".")
        return FieldRef(name=name, entity=entity)
    return FieldRef(name=spec)


# --------------------------------------------------------------------------- #
# Filter AST
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Predicate:
    """A leaf filter: ``field <op> value`` (value omitted for null checks)."""

    field: FieldRef
    op: Comparison
    value: Scalar | tuple[Scalar, ...] = None

    def __post_init__(self) -> None:
        if self.op in (Comparison.IN, Comparison.NOT_IN):
            if not isinstance(self.value, tuple):
                raise ValueError(f"{self.op} requires a tuple value, got {type(self.value)}")
            if not self.value:
                raise ValueError(f"{self.op} requires a non-empty value tuple")
        elif self.op in (Comparison.IS_NULL, Comparison.IS_NOT_NULL):
            if self.value is not None:
                raise ValueError(f"{self.op} takes no value")
        elif isinstance(self.value, tuple):
            raise ValueError(f"{self.op} takes a scalar value, not a tuple")


@dataclass(frozen=True, slots=True)
class And:
    """Logical conjunction of sub-filters (empty == always-true)."""

    terms: tuple[FilterExpr, ...]


@dataclass(frozen=True, slots=True)
class Or:
    """Logical disjunction of sub-filters (empty == always-false)."""

    terms: tuple[FilterExpr, ...]


@dataclass(frozen=True, slots=True)
class Not:
    """Logical negation of a sub-filter."""

    term: FilterExpr


#: The recursive filter expression type.
FilterExpr = Predicate | And | Or | Not


def and_all(*exprs: FilterExpr | None) -> FilterExpr | None:
    """Conjoin filter expressions, dropping ``None`` and flattening nested ``And``.

    Returns ``None`` when nothing remains (the always-true filter), the single
    expression when only one survives, and a flattened :class:`And` otherwise.
    This is how governance row-policies are composed onto a user filter.
    """
    terms: list[FilterExpr] = []
    for expr in exprs:
        if expr is None:
            continue
        if isinstance(expr, And):
            terms.extend(expr.terms)
        else:
            terms.append(expr)
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return And(tuple(terms))


def filter_fields(expr: FilterExpr | None) -> frozenset[str]:
    """Collect every qualified field name referenced anywhere in a filter tree."""
    if expr is None:
        return frozenset()
    if isinstance(expr, Predicate):
        return frozenset({expr.field.qualified})
    if isinstance(expr, Not):
        return filter_fields(expr.term)
    # And / Or
    out: set[str] = set()
    for term in expr.terms:
        out |= filter_fields(term)
    return frozenset(out)


def evaluate_filter(expr: FilterExpr | None, row: Mapping[str, Any]) -> bool:
    """Evaluate a filter tree against a single row (the in-memory engine path).

    ``row`` is keyed by *unqualified* field name; an entity-qualified
    :class:`FieldRef` first tries its qualified key then falls back to the bare
    name, so the same filter works against joined and single-table rows.
    """
    if expr is None:
        return True
    if isinstance(expr, And):
        return all(evaluate_filter(t, row) for t in expr.terms)
    if isinstance(expr, Or):
        return any(evaluate_filter(t, row) for t in expr.terms)
    if isinstance(expr, Not):
        return not evaluate_filter(expr.term, row)
    return _eval_predicate(expr, row)


def _row_value(field_ref: FieldRef, row: Mapping[str, Any]) -> Any:
    if field_ref.qualified in row:
        return row[field_ref.qualified]
    return row.get(field_ref.name)


def _coerce_comparable(value: Any) -> Any:
    """Normalise datetimes to UTC so naive/aware comparisons don't explode."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _eval_predicate(pred: Predicate, row: Mapping[str, Any]) -> bool:
    left = _coerce_comparable(_row_value(pred.field, row))
    op = pred.op
    if op is Comparison.IS_NULL:
        return left is None
    if op is Comparison.IS_NOT_NULL:
        return left is not None
    if op in (Comparison.IN, Comparison.NOT_IN):
        assert isinstance(pred.value, tuple)
        members = {_coerce_comparable(v) for v in pred.value}
        present = left in members
        return present if op is Comparison.IN else not present
    right = _coerce_comparable(pred.value)
    if op is Comparison.EQ:
        return bool(left == right)
    if op is Comparison.NEQ:
        return bool(left != right)
    # Ordered comparisons: a null on either side is never ordered-true (SQL-ish).
    if left is None or right is None:
        return False
    if op is Comparison.GT:
        return bool(left > right)
    if op is Comparison.GTE:
        return bool(left >= right)
    if op is Comparison.LT:
        return bool(left < right)
    if op is Comparison.LTE:
        return bool(left <= right)
    raise AssertionError(f"unhandled comparison {op}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #


class SortDirection(enum.StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class OrderBy:
    """An ordering key over a query output column (a dimension or metric name)."""

    key: str
    direction: SortDirection = SortDirection.ASC


@dataclass(frozen=True, slots=True)
class GrainSelection:
    """A time dimension selected at a specific grain in a query."""

    dimension: FieldRef
    grain: TimeGrain


def freeze_filters(exprs: Iterable[FilterExpr]) -> tuple[FilterExpr, ...]:
    """Materialise an iterable of filters into a stable tuple (hashable plan input)."""
    return tuple(exprs)


__all__ = [
    "ADDITIVE_AGGREGATIONS",
    "Aggregation",
    "And",
    "Comparison",
    "DataType",
    "FieldRef",
    "FilterExpr",
    "GrainSelection",
    "Not",
    "Or",
    "OrderBy",
    "Predicate",
    "Scalar",
    "SortDirection",
    "TimeGrain",
    "and_all",
    "comparison_sql",
    "evaluate_filter",
    "filter_fields",
    "freeze_filters",
    "grain_rank",
    "is_additive",
    "is_coarser_or_equal",
    "parse_field_ref",
    "validate_identifier",
]
