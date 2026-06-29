"""Hot-path query profiler — aggregate query stats into flamegraph-style reports.

The profiler answers "where is the database time going?" by aggregating per
*query-shape* (fingerprint) statistics: call count, total/mean/p95 latency, rows
returned, and — when an EXPLAIN plan is supplied — the planner cost and whether
the plan used a sequential scan. It deliberately keys on the normalised
fingerprint so a parameterised query is one row, not thousands.

Inputs come from two places, both already in the codebase:

* :class:`~app.db.inspect.QueryPlan` (and :func:`~app.db.inspect.explain_analyze`)
  — feed a real plan via :meth:`QueryProfiler.record_plan` to capture cost +
  seq-scan + execution time.
* :class:`~app.db.engine.SlowQueryRecord` — ingest the engine's slow-query ring
  buffer via :meth:`QueryProfiler.ingest_slow_queries` so the live slow feed rolls
  up into the same per-shape stats.

Output is a :class:`HotPathReport`: the shapes ranked by total time, plus a
**flamegraph** folding of recorded call stacks. The flamegraph is the
Brendan-Gregg "folded stacks" format (``a;b;c <samples>`` per line), which renders
directly in standard flamegraph tools and is trivial to assert in a test.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.datascale.optimize.fingerprint import make_fingerprint

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.engine import SlowQueryRecord
    from app.db.inspect import QueryPlan


# --------------------------------------------------------------------------- #
# Per-shape statistics
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ShapeStat:
    """Aggregated statistics for one query shape (fingerprint)."""

    fingerprint: str
    skeleton: str
    calls: int = 0
    total_ms: float = 0.0
    rows_total: int = 0
    max_ms: float = 0.0
    min_ms: float = math.inf
    plan_cost_total: float = 0.0
    plan_samples: int = 0
    seq_scan_calls: int = 0
    # A reservoir of recent durations for percentile estimation.
    _durations: list[float] = field(default_factory=list, repr=False)

    #: Cap the duration reservoir so a hot shape does not grow unbounded.
    _RESERVOIR_CAP: int = field(default=2048, repr=False)

    def observe(self, duration_ms: float, *, rows: int = 0) -> None:
        """Record one execution of this shape."""
        self.calls += 1
        self.total_ms += duration_ms
        self.rows_total += rows
        self.max_ms = max(self.max_ms, duration_ms)
        self.min_ms = min(self.min_ms, duration_ms)
        if len(self._durations) < self._RESERVOIR_CAP:
            self._durations.append(duration_ms)

    def observe_plan(self, cost: float, *, used_seq_scan: bool) -> None:
        """Record one EXPLAIN plan's cost + seq-scan flag for this shape."""
        self.plan_cost_total += cost
        self.plan_samples += 1
        if used_seq_scan:
            self.seq_scan_calls += 1

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    @property
    def mean_rows(self) -> float:
        return self.rows_total / self.calls if self.calls else 0.0

    @property
    def mean_plan_cost(self) -> float:
        return self.plan_cost_total / self.plan_samples if self.plan_samples else 0.0

    @property
    def uses_seq_scan(self) -> bool:
        """True when any recorded plan for this shape used a sequential scan."""
        return self.seq_scan_calls > 0

    def percentile(self, p: float) -> float:
        """Nearest-rank percentile (``p`` in [0, 100]) of recorded durations."""
        if not self._durations:
            return 0.0
        ordered = sorted(self._durations)
        if p <= 0:
            return ordered[0]
        if p >= 100:
            return ordered[-1]
        rank = math.ceil(p / 100 * len(ordered)) - 1
        rank = max(0, min(rank, len(ordered) - 1))
        return ordered[rank]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint[:12],
            "skeleton": self.skeleton,
            "calls": self.calls,
            "total_ms": round(self.total_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "p95_ms": round(self.percentile(95), 3),
            "max_ms": round(self.max_ms, 3),
            "mean_rows": round(self.mean_rows, 2),
            "mean_plan_cost": round(self.mean_plan_cost, 2),
            "uses_seq_scan": self.uses_seq_scan,
        }


# --------------------------------------------------------------------------- #
# Flamegraph folding
# --------------------------------------------------------------------------- #


class FlameGraph:
    """Accumulates folded call stacks and renders them.

    A *stack* is an ordered list of frames (outermost first), e.g.
    ``["request:GET /book", "BookRepo.get", "SELECT ... FROM book"]``. Recording a
    stack with a sample weight (milliseconds or a count) increments that path. The
    :meth:`fold` output is the standard ``frame;frame;frame <weight>`` per line,
    consumable by flamegraph.pl / speedscope and easy to assert.
    """

    def __init__(self) -> None:
        self._stacks: dict[tuple[str, ...], float] = {}

    def add(self, stack: Sequence[str], weight: float = 1.0) -> None:
        """Add ``weight`` samples to the folded path ``stack``."""
        if not stack:
            return
        key = tuple(stack)
        self._stacks[key] = self._stacks.get(key, 0.0) + weight

    def fold(self) -> str:
        """Render the folded-stacks text (sorted for determinism)."""
        lines = [
            f"{';'.join(stack)} {self._fmt(weight)}"
            for stack, weight in sorted(self._stacks.items())
        ]
        return "\n".join(lines)

    @staticmethod
    def _fmt(weight: float) -> str:
        return str(int(weight)) if weight == int(weight) else f"{weight:.3f}"

    def total_weight(self) -> float:
        """Sum of all sample weights."""
        return sum(self._stacks.values())

    def tree(self) -> dict[str, Any]:
        """A nested ``{name, value, children}`` tree (for a JSON flamegraph view)."""
        root: dict[str, Any] = {"name": "root", "value": 0.0, "children": {}}
        for stack, weight in self._stacks.items():
            node = root
            node["value"] += weight
            for frame in stack:
                children = node["children"]
                if frame not in children:
                    children[frame] = {"name": frame, "value": 0.0, "children": {}}
                node = children[frame]
                node["value"] += weight
        return _materialise_tree(root)


def _materialise_tree(node: dict[str, Any]) -> dict[str, Any]:
    """Convert the dict-keyed children into a sorted list (stable output)."""
    children = [
        _materialise_tree(child)
        for _, child in sorted(node["children"].items())
    ]
    return {
        "name": node["name"],
        "value": round(node["value"], 3),
        "children": children,
    }


# --------------------------------------------------------------------------- #
# The profiler + report
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class HotPathReport:
    """A ranked view of the hottest query shapes + a flamegraph fold."""

    shapes: list[ShapeStat]
    total_ms: float
    total_calls: int
    flamegraph_folded: str

    def top(self, n: int) -> list[ShapeStat]:
        """The ``n`` hottest shapes by total time."""
        return self.shapes[:n]

    def seq_scan_offenders(self) -> list[ShapeStat]:
        """Shapes whose plans used a sequential scan (advisor candidates)."""
        return [s for s in self.shapes if s.uses_seq_scan]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round(self.total_ms, 3),
            "total_calls": self.total_calls,
            "shapes": [s.as_dict() for s in self.shapes],
            "flamegraph_folded": self.flamegraph_folded,
        }


class QueryProfiler:
    """Aggregates query observations + EXPLAIN plans into a :class:`HotPathReport`."""

    def __init__(self) -> None:
        self._shapes: dict[str, ShapeStat] = {}
        self._flame = FlameGraph()

    def _shape(self, sql: str) -> ShapeStat:
        qf = make_fingerprint(sql)
        stat = self._shapes.get(qf.hexdigest)
        if stat is None:
            stat = ShapeStat(fingerprint=qf.hexdigest, skeleton=qf.skeleton)
            self._shapes[qf.hexdigest] = stat
        return stat

    def record(
        self,
        sql: str,
        duration_ms: float,
        *,
        rows: int = 0,
        stack: Sequence[str] | None = None,
    ) -> None:
        """Record one query execution (+ optional call stack for the flamegraph)."""
        stat = self._shape(sql)
        stat.observe(duration_ms, rows=rows)
        if stack is not None:
            self._flame.add([*stack, stat.skeleton], weight=duration_ms)

    def record_plan(self, sql: str, plan: QueryPlan) -> None:
        """Record an EXPLAIN(/ANALYZE) plan for ``sql`` (cost + seq-scan + time)."""
        stat = self._shape(sql)
        stat.observe_plan(plan.total_cost, used_seq_scan=plan.used_seq_scan)
        if plan.execution_time_ms is not None:
            stat.observe(plan.execution_time_ms)

    def ingest_slow_queries(self, records: Iterable[SlowQueryRecord]) -> int:
        """Roll the engine's slow-query ring buffer into per-shape stats.

        Returns the number of records ingested.
        """
        count = 0
        for rec in records:
            stat = self._shape(rec.statement)
            rows = rec.rowcount if rec.rowcount is not None and rec.rowcount >= 0 else 0
            stat.observe(rec.duration_ms, rows=rows)
            count += 1
        return count

    def add_stack(self, stack: Sequence[str], weight: float = 1.0) -> None:
        """Add a raw call stack to the flamegraph (for non-query frames)."""
        self._flame.add(stack, weight=weight)

    def report(self) -> HotPathReport:
        """Build the ranked hot-path report."""
        shapes = sorted(self._shapes.values(), key=lambda s: s.total_ms, reverse=True)
        return HotPathReport(
            shapes=shapes,
            total_ms=sum(s.total_ms for s in shapes),
            total_calls=sum(s.calls for s in shapes),
            flamegraph_folded=self._flame.fold(),
        )

    def flamegraph(self) -> FlameGraph:
        """The underlying flamegraph accumulator."""
        return self._flame

    def reset(self) -> None:
        """Clear all aggregated state."""
        self._shapes.clear()
        self._flame = FlameGraph()


__all__ = [
    "FlameGraph",
    "HotPathReport",
    "QueryProfiler",
    "ShapeStat",
]
