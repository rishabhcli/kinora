"""The query planner — lowers a logical plan to a physical plan.

:class:`Planner.plan` walks a :class:`~app.lakehouse.warehouse.logical.LogicalPlan`
and produces a :class:`~app.lakehouse.warehouse.physical.PhysicalOperator` tree. A
:class:`TableResolver` supplies the batches for each ``Scan`` node (the engine
backs it with a catalog; tests back it with in-memory data) — the planner pushes
the scan's predicate/projection/snapshot down to that resolver so pruning happens
at the source.

Two simple, sound logical optimisations run first (:func:`optimize`):

* **predicate pushdown** — a ``Filter`` whose predicate is expressible as a
  pushdown :class:`~app.lakehouse.warehouse.predicate.Predicate` and sits directly
  above a ``Scan`` is folded into the scan (and a redundant residual filter
  dropped only when fully convertible).
* **projection pushdown** — the set of columns a plan actually needs is propagated
  down to the ``Scan`` so unused columns are never decoded.

The optimiser is deliberately conservative: anything it cannot prove safe is left
as an explicit operator, so results never change — only work is avoided.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.expr import (
    BoolKind,
    BoolOp,
    Column,
    CompareOp,
    Comparison,
    Expr,
    Literal,
)
from app.lakehouse.warehouse.logical import (
    Aggregate,
    Filter,
    Join,
    Limit,
    LogicalPlan,
    Project,
    Scan,
    Sort,
)
from app.lakehouse.warehouse.physical import (
    FilterExec,
    HashAggregateExec,
    HashJoinExec,
    InMemoryScanExec,
    LimitExec,
    PhysicalOperator,
    ProjectExec,
    SortExec,
)
from app.lakehouse.warehouse.predicate import (
    And,
    Compare,
)
from app.lakehouse.warehouse.predicate import (
    CompareOp as PCompareOp,
)
from app.lakehouse.warehouse.predicate import (
    Predicate as PushPredicate,
)


class TableResolver(Protocol):
    """Supplies scan batches for a ``Scan`` node with pushdown applied."""

    def resolve_scan(self, scan: Scan) -> list[RecordBatch]:
        ...


_COMPARE_MAP = {
    CompareOp.EQ: PCompareOp.EQ,
    CompareOp.NE: PCompareOp.NE,
    CompareOp.LT: PCompareOp.LT,
    CompareOp.LE: PCompareOp.LE,
    CompareOp.GT: PCompareOp.GT,
    CompareOp.GE: PCompareOp.GE,
}


def to_pushdown(expr: Expr) -> PushPredicate | None:
    """Convert a filter expression to a pushdown predicate, or ``None`` if it can't.

    Handles ``column <op> literal`` (either operand order) and ``AND`` of
    convertible children. ``OR`` / arithmetic / non-trivial expressions return
    ``None`` (kept as a residual filter).
    """
    if isinstance(expr, Comparison):
        col_name, value, op = _normalise_comparison(expr)
        if col_name is None:
            return None
        return Compare(col_name, _COMPARE_MAP[op], value)
    if isinstance(expr, BoolOp) and expr.kind is BoolKind.AND:
        parts: list[PushPredicate] = []
        for operand in expr.operands:
            converted = to_pushdown(operand)
            if converted is None:
                return None
            parts.append(converted)
        return And(tuple(parts))
    return None


def _normalise_comparison(cmp: Comparison) -> tuple[str | None, object, CompareOp]:
    left, right, op = cmp.left, cmp.right, cmp.op
    if isinstance(left, Column) and isinstance(right, Literal):
        return left.name, right.value, op
    if isinstance(left, Literal) and isinstance(right, Column):
        return right.name, left.value, _flip(op)
    return None, None, op


def _flip(op: CompareOp) -> CompareOp:
    return {
        CompareOp.EQ: CompareOp.EQ,
        CompareOp.NE: CompareOp.NE,
        CompareOp.LT: CompareOp.GT,
        CompareOp.GT: CompareOp.LT,
        CompareOp.LE: CompareOp.GE,
        CompareOp.GE: CompareOp.LE,
    }[op]


# --------------------------------------------------------------------------- #
# Logical optimisation.
# --------------------------------------------------------------------------- #


def optimize(plan: LogicalPlan) -> LogicalPlan:
    """Apply projection + predicate pushdown. Result is logically equivalent.

    Projection pushdown threads *required output columns* top-down: the root needs
    its full output, and each operator maps "columns required from me" to "columns
    required from my child", narrowing the scan projection at the leaves.
    """
    pushed = _push_predicates(plan)
    return _push_projection(pushed, set(pushed.output_schema().names))


def _push_predicates(plan: LogicalPlan) -> LogicalPlan:
    if isinstance(plan, Filter) and isinstance(plan.input, Scan):
        pushed = to_pushdown(plan.predicate)
        if pushed is not None:
            scan = plan.input
            merged = pushed if scan.predicate is None else And((scan.predicate, pushed))
            new_scan = Scan(
                table_name=scan.table_name,
                table_schema=scan.table_schema,
                projection=scan.projection,
                predicate=merged,
                snapshot_id=scan.snapshot_id,
            )
            # The pushdown is conservative (statistics-based) so the residual filter
            # is kept to guarantee exact row-level correctness.
            return Filter(new_scan, plan.predicate)
    return _map_children(plan, _push_predicates)


def _map_children(
    plan: LogicalPlan, fn: Callable[[LogicalPlan], LogicalPlan]
) -> LogicalPlan:
    if isinstance(plan, Filter):
        return Filter(fn(plan.input), plan.predicate)
    if isinstance(plan, Project):
        return Project(fn(plan.input), plan.expressions)
    if isinstance(plan, Aggregate):
        return Aggregate(fn(plan.input), plan.group_by, plan.aggregates)
    if isinstance(plan, Sort):
        return Sort(fn(plan.input), plan.keys)
    if isinstance(plan, Limit):
        return Limit(fn(plan.input), plan.count, plan.offset)
    if isinstance(plan, Join):
        return Join(fn(plan.left), fn(plan.right), plan.on, plan.how, plan.right_prefix)
    return plan


def _push_projection(plan: LogicalPlan, required: set[str]) -> LogicalPlan:
    """Rewrite ``plan`` so its scans read only the columns ``required`` above them.

    ``required`` is the set of *this node's output* columns the parent needs.
    """
    if isinstance(plan, Scan):
        # Read only columns still required above this scan (plus any the pushed
        # predicate needs); never read fewer than one column.
        wanted = set(required)
        if plan.predicate is not None:
            wanted |= plan.predicate.columns()
        cols = [c for c in plan.table_schema.names if c in wanted]
        if not cols:
            cols = [plan.table_schema.names[0]]
        return Scan(
            table_name=plan.table_name,
            table_schema=plan.table_schema,
            projection=tuple(cols),
            predicate=plan.predicate,
            snapshot_id=plan.snapshot_id,
        )
    if isinstance(plan, Filter):
        child_req: set[str] = (required | plan.predicate.columns()) & set(
            plan.input.output_schema().names
        )
        return Filter(_push_projection(plan.input, child_req), plan.predicate)
    if isinstance(plan, Project):
        child_req = set()
        for _name, expr in plan.expressions:
            child_req |= expr.columns()
        return Project(_push_projection(plan.input, child_req), plan.expressions)
    if isinstance(plan, Aggregate):
        child_req = set(plan.group_by)
        for agg in plan.aggregates:
            if agg.input is not None:
                child_req |= agg.input.columns()
        return Aggregate(_push_projection(plan.input, child_req), plan.group_by, plan.aggregates)
    if isinstance(plan, Sort):
        child_req = (required | {k for k, _d in plan.keys}) & set(plan.input.output_schema().names)
        return Sort(_push_projection(plan.input, child_req), plan.keys)
    if isinstance(plan, Limit):
        child_req = required & set(plan.input.output_schema().names)
        return Limit(_push_projection(plan.input, child_req), plan.count, plan.offset)
    if isinstance(plan, Join):
        # A join's output renames right-side clashes, so mapping ``required`` back
        # onto each side is fiddly. Pruning *through* a join is rarely worth the
        # risk, so conservatively require every column on both sides (the scans
        # under each side still get full predicate pushdown). Correctness first.
        left_req = set(plan.left.output_schema().names)
        right_req = set(plan.right.output_schema().names)
        return Join(
            _push_projection(plan.left, left_req),
            _push_projection(plan.right, right_req),
            plan.on,
            plan.how,
            plan.right_prefix,
        )
    return plan


# --------------------------------------------------------------------------- #
# Physical lowering.
# --------------------------------------------------------------------------- #


class Planner:
    """Lowers a logical plan to a physical operator tree against a resolver."""

    def __init__(self, resolver: TableResolver) -> None:
        self._resolver = resolver

    def plan(self, logical: LogicalPlan, *, optimize_plan: bool = True) -> PhysicalOperator:
        if optimize_plan:
            logical = optimize(logical)
        return self._lower(logical)

    def _lower(self, plan: LogicalPlan) -> PhysicalOperator:
        if isinstance(plan, Scan):
            batches = self._resolver.resolve_scan(plan)
            return InMemoryScanExec(plan.output_schema(), batches)
        if isinstance(plan, Filter):
            return FilterExec(self._lower(plan.input), plan.predicate)
        if isinstance(plan, Project):
            return ProjectExec(self._lower(plan.input), plan.expressions, plan.output_schema())
        if isinstance(plan, Aggregate):
            return HashAggregateExec(
                self._lower(plan.input), plan.group_by, plan.aggregates, plan.output_schema()
            )
        if isinstance(plan, Sort):
            return SortExec(self._lower(plan.input), plan.keys)
        if isinstance(plan, Limit):
            return LimitExec(self._lower(plan.input), plan.count, plan.offset)
        if isinstance(plan, Join):
            return HashJoinExec(
                self._lower(plan.left),
                self._lower(plan.right),
                plan.on,
                plan.how,
                plan.output_schema(),
                plan.right_prefix,
            )
        raise TypeError(f"cannot lower {type(plan).__name__}")  # pragma: no cover


__all__ = ["Planner", "TableResolver", "optimize", "to_pushdown"]
