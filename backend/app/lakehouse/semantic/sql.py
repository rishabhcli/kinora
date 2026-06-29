"""The SQL fallback — render an :class:`AggregationPlan` to parameterised SQL.

When facet A's columnar engine is unavailable (or the deployment just wants to
push the aggregation down to Postgres), the plan is rendered to a single
``SELECT ... GROUP BY`` and run through any DB-API/SQLAlchemy connection. This
module produces the *text + bound parameters*; it executes nothing itself, so it
stays pure and unit-testable (and dialect-pluggable).

Safety: identifiers (tables, columns, aliases) are validated upstream
(:func:`validate_identifier`) and only ever interpolated as bare names; *all*
literal values from filters go through bound parameters (``:p0``, ``:p1`` …), so
the renderer is injection-safe by construction. The time-grain truncation uses
``date_trunc`` (Postgres dialect, the Kinora default); a :class:`SqlDialect`
indirection keeps the door open for others.

The rendered SQL column names mirror the plan's output names exactly, so the
:class:`~app.lakehouse.semantic.executor` post-aggregation stage consumes a SQL
result identically to an in-memory one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.lakehouse.semantic.plan import (
    AggExpr,
    AggregationPlan,
    GroupKey,
    JoinStep,
)
from app.lakehouse.semantic.types import (
    Aggregation,
    And,
    Comparison,
    FieldRef,
    FilterExpr,
    Not,
    Or,
    Predicate,
    Scalar,
    TimeGrain,
    and_all,
    comparison_sql,
)


class SqlDialect:
    """Postgres-flavoured dialect hooks (the Kinora default)."""

    @staticmethod
    def quote_ident(name: str) -> str:
        # Identifiers are pre-validated to [a-z_][a-z0-9_]*; double-quote anyway
        # so reserved words (e.g. a column named "year") are safe.
        return f'"{name}"'

    @staticmethod
    def date_trunc(grain: TimeGrain, expr: str) -> str:
        return f"date_trunc('{grain.value}', {expr})"

    @staticmethod
    def aggregate(agg: Aggregation, expr: str) -> str:
        if agg is Aggregation.COUNT:
            return f"count({expr})"
        if agg is Aggregation.COUNT_DISTINCT:
            return f"count(distinct {expr})"
        if agg is Aggregation.SUM:
            return f"sum({expr})"
        if agg is Aggregation.SUM_BOOLEAN:
            # count rows where the boolean expr is true.
            return f"count(*) filter (where {expr})"
        if agg is Aggregation.AVERAGE:
            return f"avg({expr})"
        if agg is Aggregation.MIN:
            return f"min({expr})"
        if agg is Aggregation.MAX:
            return f"max({expr})"
        raise ValueError(f"unsupported aggregation {agg}")  # pragma: no cover


@dataclass
class _ParamBag:
    """Accumulates bound parameters with stable names ``p0``, ``p1`` …."""

    params: dict[str, Scalar] = field(default_factory=dict)
    _n: int = 0

    def add(self, value: Scalar) -> str:
        name = f"p{self._n}"
        self._n += 1
        self.params[name] = value
        return f":{name}"


@dataclass(frozen=True, slots=True)
class RenderedSql:
    """The rendered statement + its bound parameters."""

    sql: str
    params: dict[str, Scalar]


class SqlRenderer:
    """Renders an :class:`AggregationPlan` into a single parameterised SELECT."""

    def __init__(self, dialect: SqlDialect | None = None):
        self.dialect = dialect or SqlDialect()

    def render(self, plan: AggregationPlan) -> RenderedSql:
        bag = _ParamBag()
        q = self.dialect.quote_ident

        select_parts: list[str] = []
        group_parts: list[str] = []
        for gk in plan.group_keys:
            expr = self._group_expr(gk)
            select_parts.append(f"{expr} as {q(gk.output)}")
            group_parts.append(expr)
        for agg in plan.aggregates:
            select_parts.append(f"{self._agg_expr(agg, bag)} as {q(agg.output)}")

        from_clause = self._from_clause(plan)
        where = and_all(plan.row_filter, plan.time_window_filter)
        where_sql = self._render_filter(where, bag) if where is not None else None

        sql = f"select {', '.join(select_parts)}\nfrom {from_clause}"
        if where_sql:
            sql += f"\nwhere {where_sql}"
        if group_parts:
            sql += f"\ngroup by {', '.join(group_parts)}"
        return RenderedSql(sql=sql, params=bag.params)

    # -- clauses ----------------------------------------------------------- #

    def _group_expr(self, gk: GroupKey) -> str:
        q = self.dialect.quote_ident
        col = f"{q(gk.model)}.{q(gk.expr)}"
        if gk.grain is not None:
            return self.dialect.date_trunc(gk.grain, col)
        return col

    def _agg_expr(self, agg: AggExpr, bag: _ParamBag) -> str:
        q = self.dialect.quote_ident
        inner = "*" if agg.expr == "*" else f"{q(agg.model)}.{q(agg.expr)}"
        if agg.measure_filter is not None:
            # Measure-level filter -> a FILTER (WHERE ...) aggregate.
            cond = self._render_filter(agg.measure_filter, bag)
            base = self.dialect.aggregate(agg.agg, inner)
            # SUM_BOOLEAN already emits its own filter(...); compose carefully.
            if agg.agg is Aggregation.SUM_BOOLEAN:
                return f"count(*) filter (where ({inner}) and ({cond}))"
            return f"{base} filter (where {cond})"
        return self.dialect.aggregate(agg.agg, inner)

    def _from_clause(self, plan: AggregationPlan) -> str:
        q = self.dialect.quote_ident
        clause = f"{q(plan.base_source)} as {q(plan.base_model)}"
        for step in plan.joins:
            clause += "\n  " + self._join_clause(step)
        return clause

    def _join_clause(self, step: JoinStep) -> str:
        q = self.dialect.quote_ident
        kind = "inner join" if step.join_type == "inner" else "left join"
        # The right model's source equals its model name by our plan convention;
        # callers that need a distinct physical source should extend JoinStep.
        on = (
            f"{q(step.left_model)}.{q(step.left_key)} = "
            f"{q(step.right_model)}.{q(step.right_key)}"
        )
        return f"{kind} {q(step.right_model)} as {q(step.right_model)} on {on}"

    # -- filter rendering -------------------------------------------------- #

    def _render_filter(self, expr: FilterExpr, bag: _ParamBag) -> str:
        if isinstance(expr, And):
            if not expr.terms:
                return "true"
            return " and ".join(f"({self._render_filter(t, bag)})" for t in expr.terms)
        if isinstance(expr, Or):
            if not expr.terms:
                return "false"
            return " or ".join(f"({self._render_filter(t, bag)})" for t in expr.terms)
        if isinstance(expr, Not):
            return f"not ({self._render_filter(expr.term, bag)})"
        return self._render_predicate(expr, bag)

    def _render_predicate(self, pred: Predicate, bag: _ParamBag) -> str:
        col = self._field_sql(pred.field)
        op = pred.op
        if op is Comparison.IS_NULL:
            return f"{col} is null"
        if op is Comparison.IS_NOT_NULL:
            return f"{col} is not null"
        if op in (Comparison.IN, Comparison.NOT_IN):
            assert isinstance(pred.value, tuple)
            placeholders = ", ".join(bag.add(v) for v in pred.value)
            return f"{col} {comparison_sql(op)} ({placeholders})"
        placeholder = bag.add(self._scalar(pred.value))
        return f"{col} {comparison_sql(op)} {placeholder}"

    def _field_sql(self, ref: FieldRef) -> str:
        q = self.dialect.quote_ident
        if ref.entity is not None:
            return f"{q(ref.entity)}.{q(ref.name)}"
        return q(ref.name)

    @staticmethod
    def _scalar(value: Scalar | tuple[Scalar, ...]) -> Scalar:
        if isinstance(value, tuple):  # pragma: no cover - guarded upstream
            raise TypeError("scalar predicate received a tuple")
        return value


def render_sql(plan: AggregationPlan, dialect: SqlDialect | None = None) -> RenderedSql:
    """Convenience: render an aggregation plan to parameterised SQL."""
    return SqlRenderer(dialect).render(plan)


__all__ = ["RenderedSql", "SqlDialect", "SqlRenderer", "render_sql"]
