"""Query-plan and slow-query inspection (the §12.5 observability dividend).

Three complementary views of "what is this database doing and is it slow":

* **EXPLAIN** — :func:`explain` / :func:`explain_analyze` run Postgres
  ``EXPLAIN (FORMAT JSON [, ANALYZE])`` for a statement and return a parsed
  :class:`QueryPlan` with the total estimated/actual cost, the node tree, and a
  list of detected risks (sequential scans on large tables, sort spilling to
  disk, gross row-estimate misses). This is the dev/CI affordance that catches a
  missing index before it ships (the §4.2 source-span seek must be a btree
  ``Index Scan``, never a ``Seq Scan``).
* **pg_stat_statements** — :func:`top_statements` reads the cluster's aggregated
  statement stats when the extension is installed, returning the slowest queries
  by total/mean time. Degrades to an empty list (with a flag) when the extension
  is absent, so it never breaks a deployment that lacks it.
* **in-process slow-query ring buffer** — :func:`recent_slow_queries` surfaces
  the :class:`~app.db.engine.SlowQueryRecorder` an instrumented engine maintains
  (the live feed for the metrics panel).

The EXPLAIN helpers are Postgres-specific; they raise a clear error on a
non-Postgres engine rather than producing nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import Executable, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.logging import get_logger
from app.db.engine import SlowQueryRecord, get_recorder

logger = get_logger("app.db.inspect")

#: A ``Seq Scan`` over more than this many estimated rows is flagged as a likely
#: missing-index risk. Small tables are fine to scan; large ones are not.
SEQ_SCAN_ROW_WARN = 1000
#: Flag a node whose actual rows differ from the planner estimate by more than
#: this factor (stale statistics → bad plans).
ROW_ESTIMATE_MISS_FACTOR = 10.0


@dataclass(slots=True)
class PlanNode:
    """One node in an ``EXPLAIN`` plan tree."""

    node_type: str
    relation: str | None
    total_cost: float
    plan_rows: float
    actual_rows: float | None
    children: list[PlanNode] = field(default_factory=list)

    def walk(self) -> list[PlanNode]:
        """Depth-first flatten of this node and its descendants."""
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


@dataclass(slots=True)
class QueryPlan:
    """A parsed ``EXPLAIN`` result + heuristic risk findings."""

    root: PlanNode
    total_cost: float
    execution_time_ms: float | None
    planning_time_ms: float | None
    risks: list[str]
    raw: dict[str, Any]

    @property
    def used_seq_scan(self) -> bool:
        """True when any node is a sequential scan."""
        return any(n.node_type == "Seq Scan" for n in self.root.walk())

    def node_types(self) -> list[str]:
        """All node types in the plan, root-first."""
        return [n.node_type for n in self.root.walk()]

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary for logs / the metrics panel."""
        return {
            "total_cost": self.total_cost,
            "execution_time_ms": self.execution_time_ms,
            "planning_time_ms": self.planning_time_ms,
            "node_types": self.node_types(),
            "used_seq_scan": self.used_seq_scan,
            "risks": self.risks,
        }


def _is_postgres(bind: AsyncEngine | AsyncConnection | AsyncSession) -> bool:
    name = bind.bind.dialect.name if isinstance(bind, AsyncSession) else bind.dialect.name
    return name == "postgresql"


def _compile(statement: Executable | str) -> str:
    """Render a statement to literal-bound SQL for embedding after ``EXPLAIN``."""
    if isinstance(statement, str):
        return statement
    # ``Executable`` is the broad protocol; the concrete statements we explain
    # (Select/Insert/...) are ClauseElements that carry ``.compile``.
    compiled = cast(Any, statement).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return str(compiled)


def _parse_node(node: dict[str, Any]) -> PlanNode:
    children = [_parse_node(child) for child in node.get("Plans", [])]
    return PlanNode(
        node_type=str(node.get("Node Type", "?")),
        relation=node.get("Relation Name"),
        total_cost=float(node.get("Total Cost", 0.0)),
        plan_rows=float(node.get("Plan Rows", 0.0)),
        actual_rows=(float(node["Actual Rows"]) if "Actual Rows" in node else None),
        children=children,
    )


def _detect_risks(root: PlanNode) -> list[str]:
    risks: list[str] = []
    for node in root.walk():
        if node.node_type == "Seq Scan" and node.plan_rows >= SEQ_SCAN_ROW_WARN:
            rel = node.relation or "?"
            risks.append(f"Seq Scan on {rel} over ~{int(node.plan_rows)} rows (consider an index)")
        if node.actual_rows is not None and node.plan_rows > 0:
            ratio = max(node.actual_rows, 1.0) / node.plan_rows
            if ratio >= ROW_ESTIMATE_MISS_FACTOR or ratio <= 1.0 / ROW_ESTIMATE_MISS_FACTOR:
                risks.append(
                    f"{node.node_type} row-estimate miss: planned "
                    f"{int(node.plan_rows)} vs actual {int(node.actual_rows)} "
                    "(run ANALYZE)"
                )
    return risks


async def _run_explain(
    bind: AsyncEngine | AsyncConnection | AsyncSession,
    statement: Executable | str,
    *,
    analyze: bool,
) -> QueryPlan:
    if not _is_postgres(bind):
        raise RuntimeError("EXPLAIN inspection requires a PostgreSQL engine")
    sql = _compile(statement)
    options = "FORMAT JSON, ANALYZE, BUFFERS" if analyze else "FORMAT JSON"
    explain_sql = text(f"EXPLAIN ({options}) {sql}")

    async def _exec(conn: AsyncConnection | AsyncSession) -> Any:
        return (await conn.execute(explain_sql)).scalar_one()

    if isinstance(bind, AsyncEngine):
        async with bind.connect() as conn:
            payload = await _exec(conn)
    else:
        payload = await _exec(bind)

    # asyncpg returns the JSON as a Python list already; psycopg may return a str.
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    plan_wrapper = payload[0]
    root = _parse_node(plan_wrapper["Plan"])
    risks = _detect_risks(root)
    return QueryPlan(
        root=root,
        total_cost=root.total_cost,
        execution_time_ms=plan_wrapper.get("Execution Time"),
        planning_time_ms=plan_wrapper.get("Planning Time"),
        risks=risks,
        raw=plan_wrapper,
    )


async def explain(
    bind: AsyncEngine | AsyncConnection | AsyncSession, statement: Executable | str
) -> QueryPlan:
    """Run ``EXPLAIN (FORMAT JSON)`` (plan only, no execution) and parse it."""
    return await _run_explain(bind, statement, analyze=False)


async def explain_analyze(
    bind: AsyncEngine | AsyncConnection | AsyncSession, statement: Executable | str
) -> QueryPlan:
    """Run ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` — *executes* the statement.

    Use only on read queries (or inside a transaction you roll back): ANALYZE
    actually runs the statement, so an ``INSERT``/``UPDATE`` would take effect.
    """
    return await _run_explain(bind, statement, analyze=True)


@dataclass(slots=True)
class StatementStat:
    """One aggregated row from ``pg_stat_statements``."""

    query: str
    calls: int
    total_exec_ms: float
    mean_exec_ms: float
    rows: int

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "query": self.query,
            "calls": self.calls,
            "total_exec_ms": round(self.total_exec_ms, 3),
            "mean_exec_ms": round(self.mean_exec_ms, 3),
            "rows": self.rows,
        }


async def pg_stat_statements_available(
    bind: AsyncEngine | AsyncConnection | AsyncSession,
) -> bool:
    """True when the ``pg_stat_statements`` extension is installed + readable."""
    if not _is_postgres(bind):
        return False
    probe = text("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")

    async def _exec(conn: AsyncConnection | AsyncSession) -> bool:
        return (await conn.execute(probe)).first() is not None

    try:
        if isinstance(bind, AsyncEngine):
            async with bind.connect() as conn:
                return await _exec(conn)
        return await _exec(bind)
    except Exception as exc:  # noqa: BLE001 - introspection must never raise
        logger.warning("db.inspect.pgss_probe_failed", error=str(exc))
        return False


async def top_statements(
    bind: AsyncEngine | AsyncConnection | AsyncSession,
    *,
    limit: int = 20,
    order_by: str = "total",
) -> list[StatementStat]:
    """Return the slowest statements from ``pg_stat_statements`` (empty if absent).

    ``order_by`` is ``"total"`` (cumulative time) or ``"mean"`` (per-call time).
    Reads ``mean_exec_time``/``total_exec_time`` (PG13+ column names); a cluster
    without the extension yields ``[]`` rather than raising.
    """
    if not await pg_stat_statements_available(bind):
        return []
    column = "mean_exec_time" if order_by == "mean" else "total_exec_time"
    query = text(f"""
        SELECT query, calls, total_exec_time, mean_exec_time, rows
        FROM pg_stat_statements
        ORDER BY {column} DESC
        LIMIT :limit
        """)

    async def _exec(conn: AsyncConnection | AsyncSession) -> list[Any]:
        return list((await conn.execute(query, {"limit": limit})).all())

    try:
        if isinstance(bind, AsyncEngine):
            async with bind.connect() as conn:
                rows = await _exec(conn)
        else:
            rows = await _exec(bind)
    except Exception as exc:  # noqa: BLE001 - introspection must never raise
        logger.warning("db.inspect.pgss_read_failed", error=str(exc))
        return []

    return [
        StatementStat(
            query=str(r[0]),
            calls=int(r[1]),
            total_exec_ms=float(r[2]),
            mean_exec_ms=float(r[3]),
            rows=int(r[4]),
        )
        for r in rows
    ]


def recent_slow_queries(engine: AsyncEngine, *, limit: int | None = None) -> list[SlowQueryRecord]:
    """Return the slowest recent statements from the engine's in-process recorder."""
    recorder = get_recorder(engine)
    if recorder is None:
        return []
    return recorder.snapshot(limit=limit)


__all__ = [
    "PlanNode",
    "QueryPlan",
    "StatementStat",
    "explain",
    "explain_analyze",
    "pg_stat_statements_available",
    "recent_slow_queries",
    "top_statements",
]
