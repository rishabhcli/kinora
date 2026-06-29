"""Scatter-gather executor: run a plan across shards and merge the partials.

The planner says *what* each shard must do and *how* to combine the results;
this module does it. It fans the per-shard subqueries out concurrently, then runs
the gather recipe the plan chose:

* **PASSTHROUGH** — one shard; return its rows verbatim.
* **CONCAT** — union the per-shard row lists (unordered scatter).
* **MERGE_SORT** — a true k-way merge on the order keys using a heap, then apply
  the *global* offset/limit. Because each shard already returned ``offset+limit``
  rows sorted locally, the heap merge yields the exact global ordering and we
  stop after ``offset+limit`` merged rows.
* **AGGREGATE** — fold the distributive partials (SUM of sums, MIN of mins, …)
  and finish algebraic ones (AVG = ΣSUM / ΣCOUNT).
* **GROUP_AGGREGATE** — bucket partials by the group key, fold per group.

The actual *per-shard execution* is abstracted behind :class:`ShardExecutor`
(``async def fetch(shard_id, subquery, query) -> list[Row]``). Production wires a
:class:`SessionShardExecutor` that opens a session on the shard's engine and runs
SQL; tests wire a :class:`FakeShardExecutor` backed by in-memory rows. The merge
logic is therefore proven deterministically without a database.

Fan-out is concurrent (``asyncio.gather``) with per-shard error capture: a
``partial_results`` mode lets a scatter return what it could reach plus the list
of shards that failed, which is what an availability-first read path wants. A
``fail_fast`` mode raises on the first shard error, which is what a correctness-
first write/aggregate path wants.
"""

from __future__ import annotations

import asyncio
import enum
import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.logging import get_logger
from app.datascale.sharding.planner import (
    Aggregate,
    AggregateOp,
    GatherMode,
    LogicalQuery,
    ScatterPlan,
    ShardSubquery,
    SortDir,
    SortKey,
)

logger = get_logger("app.datascale.sharding.executor")

#: One result row — a mapping from column name to value.
Row = Mapping[str, Any]


class ShardExecutor(Protocol):
    """Runs one shard's subquery and returns its rows.

    Implementations own the binding from the abstract :class:`LogicalQuery` +
    :class:`ShardSubquery` to actual per-shard SQL (or, in tests, to in-memory
    rows). The executor must already apply the per-shard ``LIMIT`` it is told to.
    """

    async def fetch(
        self, shard_id: str, subquery: ShardSubquery, query: LogicalQuery
    ) -> list[Row]: ...


class FailureMode(enum.Enum):
    """How the executor handles a per-shard failure during a scatter."""

    #: Raise on the first shard error (correctness-first: writes, exact aggregates).
    FAIL_FAST = "fail_fast"
    #: Return reachable shards' rows + record failures (availability-first reads).
    PARTIAL = "partial"


@dataclass(slots=True)
class ShardFailure:
    """A captured per-shard failure (for PARTIAL mode reporting)."""

    shard_id: str
    error: str


@dataclass(slots=True)
class GatherResult:
    """The merged result of a scatter-gather run.

    ``rows`` is the final, gather-merged result set. ``failures`` lists shards
    that errored (empty in FAIL_FAST since the first failure raises).
    ``shards_queried`` / ``shards_succeeded`` are observability counters.
    """

    rows: list[Row]
    failures: list[ShardFailure] = field(default_factory=list)
    shards_queried: int = 0
    shards_succeeded: int = 0

    @property
    def partial(self) -> bool:
        """True iff some shards failed (the result is incomplete)."""
        return bool(self.failures)


@dataclass(slots=True)
class ScatterGatherExecutor:
    """Executes a :class:`ScatterPlan` via a :class:`ShardExecutor` and merges."""

    shard_executor: ShardExecutor
    failure_mode: FailureMode = FailureMode.FAIL_FAST

    async def execute(self, plan: ScatterPlan) -> GatherResult:
        """Run the plan: fan out, capture failures, then gather/merge."""
        partials, failures = await self._fan_out(plan)
        succeeded = len(partials)
        rows = self._gather(plan, [rows for _, rows in partials])
        return GatherResult(
            rows=rows,
            failures=failures,
            shards_queried=len(plan.subqueries),
            shards_succeeded=succeeded,
        )

    # -- fan-out ------------------------------------------------------------- #

    async def _fan_out(
        self, plan: ScatterPlan
    ) -> tuple[list[tuple[str, list[Row]]], list[ShardFailure]]:
        async def run(sq: ShardSubquery) -> tuple[str, list[Row] | BaseException]:
            try:
                rows = await self.shard_executor.fetch(sq.shard_id, sq, plan.query)
                return sq.shard_id, rows
            except Exception as exc:  # noqa: BLE001 - captured per failure_mode
                return sq.shard_id, exc

        results = await asyncio.gather(*(run(sq) for sq in plan.subqueries))
        partials: list[tuple[str, list[Row]]] = []
        failures: list[ShardFailure] = []
        for shard_id, outcome in results:
            if isinstance(outcome, BaseException):
                if self.failure_mode is FailureMode.FAIL_FAST:
                    logger.warning("scatter.shard_failed", shard=shard_id, error=str(outcome))
                    raise outcome
                failures.append(ShardFailure(shard_id=shard_id, error=str(outcome)))
                logger.warning("scatter.shard_failed_partial", shard=shard_id, error=str(outcome))
            else:
                partials.append((shard_id, outcome))
        return partials, failures

    # -- gather -------------------------------------------------------------- #

    def _gather(self, plan: ScatterPlan, partials: Sequence[list[Row]]) -> list[Row]:
        mode = plan.gather_mode
        if mode is GatherMode.PASSTHROUGH:
            return list(partials[0]) if partials else []
        if mode is GatherMode.CONCAT:
            return self._concat(partials, plan)
        if mode is GatherMode.MERGE_SORT:
            return self._merge_sort(partials, plan)
        if mode is GatherMode.AGGREGATE:
            return self._aggregate(partials, plan)
        if mode is GatherMode.GROUP_AGGREGATE:
            return self._group_aggregate(partials, plan)
        raise ValueError(f"unknown gather mode: {mode}")  # pragma: no cover

    def _concat(self, partials: Sequence[list[Row]], plan: ScatterPlan) -> list[Row]:
        rows: list[Row] = []
        for part in partials:
            rows.extend(part)
        return self._apply_global_limit(rows, plan)

    def _merge_sort(self, partials: Sequence[list[Row]], plan: ScatterPlan) -> list[Row]:
        """k-way merge on the order keys, then apply global offset/limit.

        Each partial is assumed locally sorted by the order keys (the per-shard
        query applied the same ``ORDER BY``). We push the head of each partial
        into a heap keyed by the sort tuple, pop the global minimum, and advance
        that partial — classic merge. A monotonically increasing sequence number
        breaks ties stably and keeps un-comparable row dicts off the heap's
        comparison path.
        """
        order = plan.query.order_by
        heap: list[tuple[tuple[Any, ...], int, int, int]] = []
        # heap entry: (sort_key, seq, partial_index, row_index)
        seq = 0
        for pi, part in enumerate(partials):
            if part:
                heap.append((_sort_tuple(part[0], order), seq, pi, 0))
                seq += 1
        heapq.heapify(heap)

        merged: list[Row] = []
        limit = plan.global_limit
        offset = plan.global_offset
        # We only need offset+limit rows total.
        needed = None if limit is None else offset + limit
        while heap:
            _, _, pi, ri = heapq.heappop(heap)
            merged.append(partials[pi][ri])
            if needed is not None and len(merged) >= needed:
                break
            nxt = ri + 1
            if nxt < len(partials[pi]):
                heapq.heappush(heap, (_sort_tuple(partials[pi][nxt], order), seq, pi, nxt))
                seq += 1
        return self._apply_global_limit(merged, plan)

    def _apply_global_limit(self, rows: list[Row], plan: ScatterPlan) -> list[Row]:
        start = plan.global_offset
        if plan.global_limit is None:
            return rows[start:] if start else rows
        return rows[start : start + plan.global_limit]

    def _aggregate(self, partials: Sequence[list[Row]], plan: ScatterPlan) -> list[Row]:
        """Fold a single-group aggregate over the per-shard partial rows."""
        # Each partial is expected to have a single row of the effective
        # aggregates (SUM/COUNT/MIN/MAX after AVG rewrite).
        partial_rows = [part[0] for part in partials if part]
        folded = _fold_aggregates(plan.effective_aggregates, partial_rows)
        finished = _finish_algebraic(plan.query.aggregates, folded)
        return [finished]

    def _group_aggregate(self, partials: Sequence[list[Row]], plan: ScatterPlan) -> list[Row]:
        """Re-group partial rows by the group key and fold aggregates per group."""
        group_keys = tuple(plan.query.group_by)
        buckets: dict[tuple[Any, ...], list[Row]] = {}
        order: list[tuple[Any, ...]] = []
        for part in partials:
            for row in part:
                gk = tuple(row.get(k) for k in group_keys)
                if gk not in buckets:
                    buckets[gk] = []
                    order.append(gk)
                buckets[gk].append(row)
        out: list[Row] = []
        for gk in order:
            folded = _fold_aggregates(plan.effective_aggregates, buckets[gk])
            finished = _finish_algebraic(plan.query.aggregates, folded)
            row = dict(zip(group_keys, gk, strict=True))
            row.update(finished)
            out.append(row)
        # Group results can still be ordered/limited globally.
        if plan.query.order_by:
            out = _sort_rows(out, plan.query.order_by)
        return self._apply_global_limit(out, plan)


# --------------------------------------------------------------------------- #
# Pure merge helpers (no I/O — unit-testable in isolation)
# --------------------------------------------------------------------------- #


def _sort_tuple(row: Row, order: Sequence[SortKey]) -> tuple[Any, ...]:
    """Build a comparable tuple for a row under the order keys.

    Descending keys are negated for numbers / reversed for the heap by wrapping
    in a :class:`_Reversed` shim so the min-heap yields the correct global order
    for mixed asc/desc. ``None`` sorts last (consistent, total order).
    """
    out: list[Any] = []
    for key in order:
        value = row.get(key.field)
        wrapped: Any = _SortValue(value)
        out.append(_Reversed(wrapped) if key.direction is SortDir.DESC else wrapped)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class _SortValue:
    """A total-order wrapper: ``None`` sorts after every real value."""

    value: Any

    def __lt__(self, other: _SortValue) -> bool:
        if self.value is None:
            return False
        if other.value is None:
            return True
        return bool(self.value < other.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SortValue) and self.value == other.value


@dataclass(frozen=True, slots=True)
class _Reversed:
    """Invert the ordering of a comparable for DESC merge keys."""

    inner: _SortValue

    def __lt__(self, other: _Reversed) -> bool:
        return other.inner < self.inner

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reversed) and self.inner == other.inner


def _sort_rows(rows: list[Row], order: Sequence[SortKey]) -> list[Row]:
    """Stable full sort of materialised rows by the order keys."""
    return sorted(rows, key=lambda r: _sort_tuple(r, order))


def _fold_aggregates(aggregates: Sequence[Aggregate], rows: Sequence[Row]) -> dict[str, Any]:
    """Fold distributive aggregate partials across rows into one result dict."""
    out: dict[str, Any] = {}
    for agg in aggregates:
        name = agg.output_name
        values = [r.get(name) for r in rows if r.get(name) is not None]
        out[name] = _fold_one(agg.op, values)
    return out


def _fold_one(op: AggregateOp, values: Sequence[Any]) -> Any:
    if op in (AggregateOp.COUNT, AggregateOp.SUM):
        return sum(values) if values else 0
    if op is AggregateOp.MIN:
        return min(values) if values else None
    if op is AggregateOp.MAX:
        return max(values) if values else None
    # Holistic ops are flagged by the planner; a best-effort fold returns None.
    return None


def _finish_algebraic(
    original: Sequence[Aggregate], folded: Mapping[str, Any]
) -> dict[str, Any]:
    """Turn folded partials into the user-facing aggregate row.

    Distributive aggregates pass through under their output name. Each AVG is
    reconstructed from its ``__avg_sum_*`` / ``__avg_cnt_*`` helpers and emitted
    under the AVG's own output name; the helper columns are dropped.
    """
    out: dict[str, Any] = {}
    for agg in original:
        if agg.op is AggregateOp.AVG:
            base = agg.field or "value"
            total = folded.get(f"__avg_sum_{base}", 0)
            count = folded.get(f"__avg_cnt_{base}", 0)
            out[agg.output_name] = (total / count) if count else None
        else:
            out[agg.output_name] = folded.get(agg.output_name)
    return out


# --------------------------------------------------------------------------- #
# Test / dev shard executor (deterministic, in-memory)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FakeShardExecutor:
    """An in-memory :class:`ShardExecutor` for deterministic tests.

    Holds ``rows_by_shard: {shard_id: [row, ...]}`` and, for aggregate plans,
    computes the per-shard aggregate partial rows itself so the gather logic is
    exercised end-to-end without SQL. ``fail_shards`` forces a chosen shard to
    raise (to test partial/fail-fast paths). Ordered queries get a per-shard
    local sort + per-shard limit applied here, mimicking a real ``ORDER BY ...
    LIMIT`` pushed to the shard.
    """

    rows_by_shard: Mapping[str, list[Row]]
    fail_shards: frozenset[str] = frozenset()

    async def fetch(
        self, shard_id: str, subquery: ShardSubquery, query: LogicalQuery
    ) -> list[Row]:
        if shard_id in self.fail_shards:
            raise RuntimeError(f"shard {shard_id} unavailable")
        rows = list(self.rows_by_shard.get(shard_id, []))
        if query.aggregates:
            return self._aggregate_partials(rows, query)
        if query.order_by:
            rows = _sort_rows(rows, query.order_by)
        if subquery.per_shard_limit is not None:
            rows = rows[: subquery.per_shard_limit]
        return rows

    def _aggregate_partials(self, rows: Sequence[Row], query: LogicalQuery) -> list[Row]:
        """Compute this shard's aggregate partials, one row per *local* group.

        A real shard runs the same ``GROUP BY`` locally and returns one partial
        row per group it holds; the gather then re-groups across shards. With no
        ``GROUP BY`` there is a single (global) group, so one partial row.
        """
        group_keys = tuple(query.group_by)
        if not group_keys:
            return [self._one_partial(rows, query, group_values=())]
        buckets: dict[tuple[Any, ...], list[Row]] = {}
        order: list[tuple[Any, ...]] = []
        for row in rows:
            gk = tuple(row.get(k) for k in group_keys)
            if gk not in buckets:
                buckets[gk] = []
                order.append(gk)
            buckets[gk].append(row)
        return [self._one_partial(buckets[gk], query, group_values=gk) for gk in order]

    def _one_partial(
        self, rows: Sequence[Row], query: LogicalQuery, *, group_values: tuple[Any, ...]
    ) -> Row:
        """One aggregate partial row: the group key columns + helper aggregates.

        Effective aggregates were AVG-rewritten in the plan, but FakeShard sees
        the original query; we recompute the same helper columns the gather
        expects (SUM/COUNT/MIN/MAX, plus AVG's ``__avg_sum_*``/``__avg_cnt_*``).
        """
        out: dict[str, Any] = dict(zip(query.group_by, group_values, strict=True))
        for agg in query.aggregates:
            field_name = agg.field
            vals: list[Any] = [
                r.get(field_name) for r in rows if field_name and r.get(field_name) is not None
            ]
            if agg.op is AggregateOp.COUNT:
                out[agg.output_name] = len(rows) if field_name is None else len(vals)
            elif agg.op is AggregateOp.SUM:
                out[agg.output_name] = sum(vals) if vals else 0
            elif agg.op is AggregateOp.MIN:
                out[agg.output_name] = min(vals) if vals else None
            elif agg.op is AggregateOp.MAX:
                out[agg.output_name] = max(vals) if vals else None
            elif agg.op is AggregateOp.AVG:
                base = field_name or "value"
                out[f"__avg_sum_{base}"] = sum(vals) if vals else 0
                out[f"__avg_cnt_{base}"] = len(vals)
        return out


__all__ = [
    "FailureMode",
    "FakeShardExecutor",
    "GatherResult",
    "Row",
    "ScatterGatherExecutor",
    "ShardExecutor",
    "ShardFailure",
]
