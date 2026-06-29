"""Cross-shard query planner: a logical query → a fan-out execution plan.

A single-shard query is trivial — route the key, run it, done. The interesting
work is a query whose answer is spread across shards: ``ORDER BY ... LIMIT n``,
``COUNT(*)``, ``AVG(x)``, ``GROUP BY``. The planner's job is to describe, *before
any execution*, exactly how the gather stage must combine the per-shard partials
so the merged result equals what a single big table would have returned.

The hard cases and how the plan encodes them:

* **LIMIT push-down.** A global ``ORDER BY k LIMIT n`` cannot be satisfied by
  asking each shard for its top ``n/shards`` — the global top-n could all live on
  one shard. The plan pushes ``LIMIT n`` (the *full* limit, ignoring offset's
  shard-locality) to every shard, then the gather does a k-way merge and keeps
  the global top ``offset+n``. With an offset, each shard must return
  ``offset + n`` rows so the merge has enough to skip past.

* **Aggregate decomposition.** ``COUNT``/``SUM``/``MIN``/``MAX`` are
  *distributive* — sum the per-shard partials (max of maxes, etc.). ``AVG`` is
  *algebraic* — rewrite to ``SUM`` and ``COUNT`` partials and divide at the
  gather. ``COUNT(DISTINCT)`` and ``MEDIAN`` are *holistic* — not decomposable
  exactly; the plan flags them so the executor can fall back to shipping the
  values (bounded) or refuse.

* **GROUP BY.** Each shard groups locally; the gather re-groups by the group key
  and re-applies the distributive/algebraic aggregates per group.

The planner is pure: it produces a :class:`ScatterPlan` data object the executor
interprets. No SQL is run here, so a plan can be unit-tested and logged
(EXPLAIN-style) without infrastructure.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.datascale.sharding.keys import ShardKeyValue
from app.datascale.sharding.router import Access, Resolution, ShardRouter


class AggregateOp(enum.Enum):
    """A supported aggregate and its decomposition class."""

    COUNT = "count"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    COUNT_DISTINCT = "count_distinct"

    @property
    def is_distributive(self) -> bool:
        """True iff the gather is a simple fold of the per-shard partials."""
        return self in (AggregateOp.COUNT, AggregateOp.SUM, AggregateOp.MIN, AggregateOp.MAX)

    @property
    def is_algebraic(self) -> bool:
        """True iff it decomposes into distributive partials (AVG → SUM,COUNT)."""
        return self is AggregateOp.AVG

    @property
    def is_holistic(self) -> bool:
        """True iff it cannot be computed exactly from bounded partials."""
        return self is AggregateOp.COUNT_DISTINCT


@dataclass(frozen=True, slots=True)
class Aggregate:
    """One aggregate in the SELECT list: ``op(field) AS alias``."""

    op: AggregateOp
    field: str | None = None  # None ⇒ COUNT(*)
    alias: str | None = None

    @property
    def output_name(self) -> str:
        if self.alias:
            return self.alias
        base = self.field or "star"
        return f"{self.op.value}_{base}"


class SortDir(enum.Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class SortKey:
    """One ``ORDER BY`` term."""

    field: str
    direction: SortDir = SortDir.ASC


@dataclass(frozen=True, slots=True)
class LogicalQuery:
    """A backend-agnostic description of a cross-shard query to plan.

    This is intentionally not SQL: it names the *table family* (which router to
    use), an optional shard-key equality (the fast single-shard path), an
    optional key range, the aggregates / group-by / order-by / limit. The
    executor binds it to actual per-shard SQL via a caller-supplied fragment.
    """

    table: str
    #: A concrete shard-key value ⇒ single-shard route. ``None`` ⇒ scatter.
    shard_key: ShardKeyValue | None = None
    #: A half-open key range ``[low, high)`` ⇒ ranged scatter.
    key_range: tuple[ShardKeyValue | None, ShardKeyValue | None] | None = None
    aggregates: Sequence[Aggregate] = field(default_factory=tuple)
    group_by: Sequence[str] = field(default_factory=tuple)
    order_by: Sequence[SortKey] = field(default_factory=tuple)
    limit: int | None = None
    offset: int = 0
    access: Access = Access.READ

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be >= 0")
        if self.offset < 0:
            raise ValueError("offset must be >= 0")
        if self.shard_key is not None and self.key_range is not None:
            raise ValueError("a query has either a shard_key or a key_range, not both")
        if self.group_by and not self.aggregates:
            raise ValueError("group_by requires at least one aggregate")


class GatherMode(enum.Enum):
    """How the gather stage combines per-shard partials."""

    #: Single shard — pass the partial straight through.
    PASSTHROUGH = "passthrough"
    #: Concatenate rows (a scatter with no ordering/aggregation).
    CONCAT = "concat"
    #: k-way merge on the order keys, then apply global offset/limit.
    MERGE_SORT = "merge_sort"
    #: Fold distributive/algebraic aggregates into one row.
    AGGREGATE = "aggregate"
    #: Re-group by the group keys then fold aggregates per group.
    GROUP_AGGREGATE = "group_aggregate"


@dataclass(frozen=True, slots=True)
class ShardSubquery:
    """The per-shard work item: which shard, and how many rows to ask it for.

    ``per_shard_limit`` is the LIMIT *pushed down* to this shard. For a global
    top-n it is ``offset + limit`` (each shard must surface enough rows for the
    merge to skip the global offset); ``None`` means no limit push-down (full
    scan needed, e.g. an exact aggregate).
    """

    shard_id: str
    per_shard_limit: int | None
    per_shard_offset: int = 0  # always 0: offset is applied globally at gather


@dataclass(frozen=True, slots=True)
class ScatterPlan:
    """The fully-resolved plan: per-shard subqueries + the gather recipe.

    Holds everything the executor needs and nothing it has to recompute: the
    target shards (already state-filtered by the router), the gather mode, the
    effective aggregates (AVG already rewritten to its SUM/COUNT helpers), and
    the global offset/limit to apply after the merge. :meth:`explain` renders a
    human-readable plan for logging / tests, mirroring an ``EXPLAIN``.
    """

    query: LogicalQuery
    subqueries: tuple[ShardSubquery, ...]
    gather_mode: GatherMode
    #: Aggregates after algebraic rewrite (what the executor asks each shard for).
    effective_aggregates: tuple[Aggregate, ...]
    global_offset: int
    global_limit: int | None
    holistic_warnings: tuple[str, ...] = ()

    @property
    def shard_ids(self) -> tuple[str, ...]:
        return tuple(sq.shard_id for sq in self.subqueries)

    @property
    def is_scatter(self) -> bool:
        return len(self.subqueries) > 1

    @property
    def is_single_shard(self) -> bool:
        return len(self.subqueries) == 1

    def explain(self) -> str:
        """A compact, EXPLAIN-style multi-line plan description."""
        lines = [
            f"ScatterPlan table={self.query.table!r} mode={self.gather_mode.value} "
            f"shards={len(self.subqueries)}"
        ]
        if self.effective_aggregates:
            aggs = ", ".join(a.output_name for a in self.effective_aggregates)
            lines.append(f"  aggregates: {aggs}")
        if self.query.group_by:
            lines.append(f"  group_by: {', '.join(self.query.group_by)}")
        if self.query.order_by:
            order = ", ".join(f"{s.field} {s.direction.value}" for s in self.query.order_by)
            lines.append(f"  order_by: {order}")
        if self.global_limit is not None or self.global_offset:
            lines.append(f"  global limit={self.global_limit} offset={self.global_offset}")
        for sq in self.subqueries:
            lines.append(f"  -> shard {sq.shard_id} per_shard_limit={sq.per_shard_limit}")
        for warn in self.holistic_warnings:
            lines.append(f"  ! {warn}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class QueryPlanner:
    """Turns a :class:`LogicalQuery` into a :class:`ScatterPlan` over a router."""

    router: ShardRouter

    def plan(self, query: LogicalQuery) -> ScatterPlan:
        """Produce the execution plan for ``query`` (pure; runs no SQL)."""
        resolution = self._resolve(query)
        gather_mode = self._gather_mode(query, resolution)
        effective_aggregates = self._rewrite_aggregates(query.aggregates)
        per_shard_limit = self._push_down_limit(query, resolution)
        subqueries = tuple(
            ShardSubquery(shard_id=sid, per_shard_limit=per_shard_limit)
            for sid in resolution.shard_ids
        )
        warnings = self._holistic_warnings(query, resolution)
        return ScatterPlan(
            query=query,
            subqueries=subqueries,
            gather_mode=gather_mode,
            effective_aggregates=effective_aggregates,
            global_offset=query.offset,
            global_limit=query.limit,
            holistic_warnings=warnings,
        )

    # -- internals ----------------------------------------------------------- #

    def _resolve(self, query: LogicalQuery) -> Resolution:
        if query.shard_key is not None:
            return self.router.route(query.shard_key, access=query.access)
        if query.key_range is not None:
            low, high = query.key_range
            return self.router.route_range(low, high, access=query.access)
        return self.router.scatter_all(access=query.access)

    def _gather_mode(self, query: LogicalQuery, resolution: Resolution) -> GatherMode:
        if query.aggregates:
            return GatherMode.GROUP_AGGREGATE if query.group_by else GatherMode.AGGREGATE
        if resolution.scatter is False and len(resolution.shard_ids) == 1:
            return GatherMode.PASSTHROUGH
        if query.order_by:
            return GatherMode.MERGE_SORT
        return GatherMode.CONCAT

    def _rewrite_aggregates(self, aggregates: Sequence[Aggregate]) -> tuple[Aggregate, ...]:
        """Decompose algebraic aggregates (AVG → SUM + COUNT helper partials).

        The gather divides the summed partials. We keep the original AVG's alias
        for the final output and add internal SUM/COUNT helpers the executor asks
        each shard for; the final divide happens at gather time.
        """
        out: list[Aggregate] = []
        for agg in aggregates:
            if agg.op is AggregateOp.AVG:
                base = agg.field or "value"
                out.append(
                    Aggregate(AggregateOp.SUM, field=agg.field, alias=f"__avg_sum_{base}")
                )
                out.append(
                    Aggregate(AggregateOp.COUNT, field=agg.field, alias=f"__avg_cnt_{base}")
                )
            else:
                out.append(agg)
        return tuple(out)

    def _push_down_limit(self, query: LogicalQuery, resolution: Resolution) -> int | None:
        """The LIMIT pushed to each shard.

        Aggregate queries scan everything (no limit). For a single shard, the
        limit+offset can be pushed verbatim. For a scatter with ordering, each
        shard must return ``offset + limit`` rows so the k-way merge has enough
        to apply the global skip; without a limit it returns everything.
        """
        if query.aggregates:
            return None
        if query.limit is None:
            return None
        full = query.offset + query.limit
        if resolution.scatter:
            return full
        # Single shard: still ask for offset+limit and slice globally so the
        # gather path is uniform (offset applied once, at the gather).
        return full

    def _holistic_warnings(self, query: LogicalQuery, resolution: Resolution) -> tuple[str, ...]:
        warnings: list[str] = []
        if resolution.scatter:
            for agg in query.aggregates:
                if agg.op.is_holistic:
                    warnings.append(
                        f"{agg.op.value}({agg.field}) is holistic; a scatter cannot "
                        "compute it exactly without shipping per-shard value sets"
                    )
        return tuple(warnings)


__all__ = [
    "Aggregate",
    "AggregateOp",
    "GatherMode",
    "LogicalQuery",
    "QueryPlanner",
    "ScatterPlan",
    "ShardSubquery",
    "SortDir",
    "SortKey",
]
