"""The execution seam — the ``QueryEngine`` Protocol + an in-memory reference.

The semantic layer never executes SQL or scans columns itself; it lowers a query
to an :class:`~app.lakehouse.semantic.plan.AggregationPlan` and hands that to a
:class:`QueryEngine`. Facet A (the warehouse query engine, a sibling package)
implements this Protocol; until it lands on disk, this module ships:

* :class:`QueryEngine` — the structural contract the compiler targets;
* :class:`AggregateResult` — the row shape the engine returns (grouped rows of
  dimension values + aggregate values);
* :class:`InMemoryEngine` — a complete, dependency-free reference engine over
  ``list[dict]`` tables. It executes the *entire* AggregationPlan (joins, grain
  truncation, measure-level filters, every aggregation) deterministically, which
  makes it both the substrate for the compiler's unit tests and a usable engine
  for small embedded datasets.

The contract is intentionally narrow: one method, ``execute_aggregation`` —
everything clever (metrics, cumulative, comparison) is the semantic layer's job
on top of the returned aggregate rows.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from app.lakehouse.semantic.plan import AggExpr, AggregationPlan, GroupKey
from app.lakehouse.semantic.types import (
    Aggregation,
    TimeGrain,
    evaluate_filter,
)


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """The grouped aggregate the engine returns.

    ``columns`` is the full ordered column list (group-key outputs then aggregate
    outputs); ``rows`` is a list of dicts keyed by those column names. Keeping it
    dict-shaped (not positional) makes the post-aggregation stage robust to column
    reordering between engine implementations.
    """

    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    def __len__(self) -> int:
        return len(self.rows)


@runtime_checkable
class QueryEngine(Protocol):
    """The structural contract a warehouse engine must satisfy (facet A seam)."""

    def execute_aggregation(self, plan: AggregationPlan) -> AggregateResult:
        """Execute one grouped aggregation and return its rows."""
        ...


# --------------------------------------------------------------------------- #
# In-memory reference engine
# --------------------------------------------------------------------------- #


class InMemoryEngine:
    """A complete, deterministic engine over in-memory tables (``list[dict]``).

    Register each model's *source* name against its rows; the engine then executes
    any :class:`AggregationPlan` the compiler produces. It is the reference
    implementation the compiler is tested against and a real (if un-indexed)
    engine for embedded datasets. Pure-Python, no third-party deps.
    """

    def __init__(self, tables: Mapping[str, Sequence[Mapping[str, Any]]] | None = None):
        self._tables: dict[str, list[dict[str, Any]]] = {}
        for source, rows in (tables or {}).items():
            self.register(source, rows)

    def register(self, source: str, rows: Iterable[Mapping[str, Any]]) -> None:
        """Register (or replace) the rows backing a physical source name."""
        self._tables[source] = [dict(r) for r in rows]

    def source(self, name: str) -> list[dict[str, Any]]:
        try:
            return self._tables[name]
        except KeyError:
            raise KeyError(f"in-memory engine has no source {name!r}") from None

    # -- the Protocol method ---------------------------------------------- #

    def execute_aggregation(self, plan: AggregationPlan) -> AggregateResult:
        rows = self._scan_and_join(plan)
        rows = [r for r in rows if evaluate_filter(plan.row_filter, r)]
        rows = [r for r in rows if evaluate_filter(plan.time_window_filter, r)]
        grouped = self._group(rows, plan.group_keys)
        out_rows: list[dict[str, Any]] = []
        for key_values, members in grouped.items():
            row: dict[str, Any] = {}
            for gk, value in zip(plan.group_keys, key_values, strict=True):
                row[gk.output] = value
            for agg in plan.aggregates:
                row[agg.output] = _aggregate(agg, members)
            out_rows.append(row)
        columns = tuple(g.output for g in plan.group_keys) + tuple(
            a.output for a in plan.aggregates
        )
        return AggregateResult(columns=columns, rows=tuple(out_rows))

    # -- join + scan ------------------------------------------------------- #

    def _scan_and_join(self, plan: AggregationPlan) -> list[dict[str, Any]]:
        """Materialise the base rows, left/inner-joining the declared chain.

        Each row is qualified twice: under bare column names *and* under
        ``<model>.<column>`` so both qualified and unqualified field refs resolve.
        many_to_one joins keep base cardinality (no fan-out), matching the
        compiler's fan-out-safety guarantee.
        """
        base_rows = [
            self._qualify(plan.base_model, r) for r in self.source(plan.base_source)
        ]
        rows = base_rows
        for step in plan.joins:
            right_source = self._model_source(plan, step.right_model)
            index: dict[Any, dict[str, Any]] = {}
            for rr in self.source(right_source):
                index[rr.get(step.right_key)] = self._qualify(step.right_model, rr)
            joined: list[dict[str, Any]] = []
            left_key_q = f"{step.left_model}.{step.left_key}"
            for lr in rows:
                match = index.get(lr.get(left_key_q, lr.get(step.left_key)))
                if match is None:
                    if step.join_type == "inner":
                        continue
                    joined.append(lr)
                else:
                    merged = dict(lr)
                    merged.update(match)
                    joined.append(merged)
            rows = joined
        return rows

    @staticmethod
    def _model_source(plan: AggregationPlan, model: str) -> str:
        # The compiler stamps each join's right model; resolving its source
        # requires the model->source map, which the plan does not carry. By
        # convention the in-memory engine registers sources under model name OR
        # source name; the SQL plan's base_source is authoritative for the base.
        # For joins we rely on the right_model also being registered as a source
        # (the test harness registers both). Fall back to the model name.
        return model

    @staticmethod
    def _qualify(model: str, row: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = dict(row)
        for k, v in row.items():
            out[f"{model}.{k}"] = v
        return out

    # -- grouping ---------------------------------------------------------- #

    @staticmethod
    def _group(
        rows: list[dict[str, Any]], group_keys: Sequence[GroupKey]
    ) -> OrderedDict[tuple[Any, ...], list[dict[str, Any]]]:
        groups: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
        for row in rows:
            key = tuple(_group_value(gk, row) for gk in group_keys)
            groups.setdefault(key, []).append(row)
        return groups


def _group_value(gk: GroupKey, row: Mapping[str, Any]) -> Any:
    raw = row.get(f"{gk.model}.{gk.expr}", row.get(gk.expr))
    if gk.grain is not None and raw is not None:
        return truncate_time(_as_datetime(raw), gk.grain)
    return raw


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"cannot interpret {value!r} as a datetime for time-grain truncation")


def truncate_time(value: datetime, grain: TimeGrain) -> datetime:
    """Floor a UTC datetime to the start of its grain bucket (pure, deterministic)."""
    value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if grain is TimeGrain.HOUR:
        return value.replace(minute=0, second=0, microsecond=0)
    if grain is TimeGrain.DAY:
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if grain is TimeGrain.WEEK:
        midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.fromordinal(midnight.toordinal() - midnight.weekday())
    if grain is TimeGrain.MONTH:
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if grain is TimeGrain.QUARTER:
        first_month = 3 * ((value.month - 1) // 3) + 1
        return value.replace(
            month=first_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    # YEAR
    return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# Aggregation evaluation
# --------------------------------------------------------------------------- #


def _aggregate(agg: AggExpr, members: list[dict[str, Any]]) -> Any:
    rows = [r for r in members if evaluate_filter(agg.measure_filter, r)]
    kind = agg.agg
    if kind is Aggregation.COUNT:
        if agg.expr == "*":
            return len(rows)
        return sum(1 for r in rows if _col(r, agg) is not None)
    if kind is Aggregation.COUNT_DISTINCT:
        return len({_col(r, agg) for r in rows if _col(r, agg) is not None})
    values = [_col(r, agg) for r in rows if _col(r, agg) is not None]
    if kind is Aggregation.SUM or kind is Aggregation.SUM_BOOLEAN:
        if kind is Aggregation.SUM_BOOLEAN:
            return sum(1 for v in values if bool(v))
        return _sum(values)
    if kind is Aggregation.AVERAGE:
        return (_sum(values) / len(values)) if values else None
    if kind is Aggregation.MIN:
        return min(values) if values else None
    if kind is Aggregation.MAX:
        return max(values) if values else None
    raise AssertionError(f"unhandled aggregation {kind}")  # pragma: no cover


def _col(row: Mapping[str, Any], agg: AggExpr) -> Any:
    return row.get(f"{agg.model}.{agg.expr}", row.get(agg.expr))


def _sum(values: list[Any]) -> Any:
    total: Any = 0
    for v in values:
        total += v
    return total


__all__ = [
    "AggregateResult",
    "InMemoryEngine",
    "QueryEngine",
    "truncate_time",
]
