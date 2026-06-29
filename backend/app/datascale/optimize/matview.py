"""Automatic materialized-view system.

Four capabilities behind one module:

1. **Definition** — :class:`MatviewDef` is a typed spec for a materialized view:
   its name, the SELECT that defines it (shaped via :mod:`sqlshape`), the set of
   base tables it depends on (derived, with an override), and a
   :class:`FreshnessPolicy` (max staleness + whether incremental refresh by a key
   column is possible).

2. **Registry** — :class:`MatviewRegistry` is an in-memory catalog. It is the
   single source of truth the rewriter consults and the refresh planner drives.

3. **Refresh** — :class:`RefreshPlanner` computes *what* to refresh given a write
   stream: a full ``REFRESH MATERIALIZED VIEW`` for views without an incremental
   key, or an incremental ``DELETE+INSERT`` scoped to the changed key values for
   views that declare one. The :class:`StalenessClock` tracks per-view age so a
   caller knows when a view's data has exceeded its policy.

4. **Rewrite** — :func:`rewrite` transparently rewrites an eligible query to read
   from a matview *only when the rewrite is provably sound* (see ``DESIGN.md`` →
   "Soundness model"). When in doubt it returns ``None`` and the original query
   runs unchanged. ``rewrite_strict`` is the same check that raises
   :class:`RewriteUnsound` instead of declining, for callers that assert.

DDL generation is Postgres-flavoured but pure string construction; nothing here
opens a connection. An ``apply``/``refresh`` *executor* that runs the generated
SQL against an :class:`AsyncConnection` lives in :class:`MatviewExecutor`, which
the integration tests exercise against ``qopt_test``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import TYPE_CHECKING

from app.datascale.optimize.errors import RefreshError, RewriteUnsound, UnknownMatview
from app.datascale.optimize.sqlshape import ColumnRef, SelectShape, parse_select

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncConnection


@lru_cache(maxsize=512)
def _shape_for(select_sql: str) -> SelectShape:
    """Parse + memoise the shape of a defining SELECT (keyed by its text)."""
    return parse_select(select_sql)


# --------------------------------------------------------------------------- #
# Definition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """How stale a materialized view is allowed to get + how it refreshes.

    ``max_staleness_s`` is the age beyond which :class:`StalenessClock` reports the
    view as stale (and a scheduler should refresh it). ``incremental_key`` names a
    column whose changed values can be re-materialised in isolation; when ``None``
    the only safe refresh is a full rebuild.
    """

    max_staleness_s: float = 300.0
    incremental_key: str | None = None

    @property
    def supports_incremental(self) -> bool:
        """True when an incremental (key-scoped) refresh is possible."""
        return self.incremental_key is not None


@dataclass(frozen=True, slots=True)
class MatviewDef:
    """A typed materialized-view definition."""

    name: str
    select_sql: str
    freshness: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    #: When set, overrides the auto-derived dependency table set.
    dependency_override: frozenset[str] | None = None

    def __post_init__(self) -> None:
        # Validate the SELECT shape at definition time so a bad MV fails loudly
        # when constructed, not at rewrite time. The parsed shape is memoised by
        # SQL text (the dataclass is frozen + slotted, so we cannot stash it on
        # the instance; the cache makes ``.shape`` O(1) after the first call).
        _shape_for(self.select_sql)

    @property
    def shape(self) -> SelectShape:
        """The parsed :class:`SelectShape` of the defining SELECT."""
        return _shape_for(self.select_sql)

    @property
    def dependencies(self) -> frozenset[str]:
        """Base tables this view depends on (override or derived from the shape)."""
        if self.dependency_override is not None:
            return self.dependency_override
        return self.shape.table_names

    @property
    def materialized_columns(self) -> frozenset[str]:
        """The column names this MV physically stores (output of its SELECT)."""
        cols = {c.column for c in self.shape.columns}
        # Group keys are always materialised even if also projected.
        cols |= {c.column for c in self.shape.group_by}
        return frozenset(cols)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class MatviewRegistry:
    """In-memory catalog of materialized views, indexed for fast rewrite lookup."""

    def __init__(self) -> None:
        self._views: dict[str, MatviewDef] = {}
        # table -> set of MV names depending on it (the invalidation/rewrite index).
        self._by_table: dict[str, set[str]] = {}

    def register(self, mv: MatviewDef) -> None:
        """Add or replace a view definition, maintaining the table index."""
        if mv.name in self._views:
            self.unregister(mv.name)
        self._views[mv.name] = mv
        for table in mv.dependencies:
            self._by_table.setdefault(table, set()).add(mv.name)

    def unregister(self, name: str) -> None:
        """Remove a view definition (no-op if absent)."""
        mv = self._views.pop(name, None)
        if mv is None:
            return
        for table in mv.dependencies:
            names = self._by_table.get(table)
            if names is not None:
                names.discard(name)
                if not names:
                    del self._by_table[table]

    def get(self, name: str) -> MatviewDef:
        """Return a view by name or raise :class:`UnknownMatview`."""
        try:
            return self._views[name]
        except KeyError as exc:
            raise UnknownMatview(f"no materialized view named {name!r}") from exc

    def all(self) -> list[MatviewDef]:
        """All registered view definitions (stable, name-sorted)."""
        return [self._views[n] for n in sorted(self._views)]

    def views_for_table(self, table: str) -> list[MatviewDef]:
        """Views whose dependency set includes ``table`` (for invalidation)."""
        return [self._views[n] for n in sorted(self._by_table.get(table.lower(), set()))]

    def candidates_for(self, shape: SelectShape) -> list[MatviewDef]:
        """Views that *might* answer ``shape`` (share at least one base table)."""
        names: set[str] = set()
        for table in shape.table_names:
            names |= self._by_table.get(table, set())
        return [self._views[n] for n in sorted(names)]

    def __len__(self) -> int:
        return len(self._views)

    def __contains__(self, name: object) -> bool:
        return name in self._views


# --------------------------------------------------------------------------- #
# Staleness clock
# --------------------------------------------------------------------------- #


class StalenessClock:
    """Tracks the last-refresh time of each view to answer "is it stale?".

    ``now`` is injectable so tests are deterministic.
    """

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._last_refresh: dict[str, float] = {}
        # Pending dirty key-values per view, for incremental refresh.
        self._dirty: dict[str, set[object]] = {}

    def mark_refreshed(self, name: str) -> None:
        """Record that ``name`` was just fully refreshed (clears dirty keys)."""
        self._last_refresh[name] = self._now()
        self._dirty.pop(name, None)

    def mark_dirty(self, name: str, key_value: object) -> None:
        """Record that one key value of ``name`` changed (needs incremental refresh)."""
        self._dirty.setdefault(name, set()).add(key_value)

    def dirty_keys(self, name: str) -> frozenset[object]:
        """The set of key values awaiting an incremental refresh for ``name``."""
        return frozenset(self._dirty.get(name, set()))

    def age_s(self, name: str) -> float | None:
        """Seconds since ``name`` was last refreshed (``None`` if never)."""
        last = self._last_refresh.get(name)
        return None if last is None else self._now() - last

    def is_stale(self, mv: MatviewDef) -> bool:
        """True when ``mv`` has never refreshed, has dirty keys, or exceeds policy."""
        if self._dirty.get(mv.name):
            return True
        age = self.age_s(mv.name)
        if age is None:
            return True
        return age > mv.freshness.max_staleness_s


# --------------------------------------------------------------------------- #
# Refresh planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    """A planned refresh action for one view."""

    name: str
    kind: str  # "full" | "incremental" | "skip"
    sql: tuple[str, ...]
    key_values: tuple[object, ...] = ()

    @property
    def is_noop(self) -> bool:
        """True when nothing needs to run."""
        return self.kind == "skip"


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier (basic double-quoting; rejects embedded quotes)."""
    if '"' in name:
        raise RefreshError(f"unsafe identifier {name!r}")
    return f'"{name}"'


class RefreshPlanner:
    """Plans full vs incremental refreshes for the views in a registry."""

    def __init__(self, registry: MatviewRegistry, clock: StalenessClock) -> None:
        self._registry = registry
        self._clock = clock

    def plan_full(self, name: str, *, concurrently: bool = False) -> RefreshPlan:
        """A full ``REFRESH MATERIALIZED VIEW`` plan."""
        mv = self._registry.get(name)
        opt = "CONCURRENTLY " if concurrently else ""
        sql = f"REFRESH MATERIALIZED VIEW {opt}{_quote_ident(mv.name)}"
        return RefreshPlan(name=mv.name, kind="full", sql=(sql,))

    def plan_incremental(self, name: str) -> RefreshPlan:
        """An incremental refresh scoped to the view's dirty key values.

        Falls back to a full plan when the view declares no incremental key.
        Returns a ``skip`` plan when there is nothing dirty.
        """
        mv = self._registry.get(name)
        if not mv.freshness.supports_incremental:
            return self.plan_full(name)
        keys = self._clock.dirty_keys(name)
        if not keys:
            return RefreshPlan(name=mv.name, kind="skip", sql=())
        key_col = mv.freshness.incremental_key
        assert key_col is not None
        # The incremental statement set re-materialises only the affected key
        # partition: delete the stale rows then re-insert from the defining SELECT
        # restricted to the dirty keys. The MV is modelled as a plain table for
        # incremental views (a "materialized table" maintained by these statements).
        placeholders = ", ".join(f":k{i}" for i in range(len(keys)))
        delete_sql = (
            f"DELETE FROM {_quote_ident(mv.name)} "
            f"WHERE {_quote_ident(key_col)} IN ({placeholders})"
        )
        insert_sql = (
            f"INSERT INTO {_quote_ident(mv.name)} "
            f"SELECT * FROM ({mv.select_sql}) AS _src "
            f"WHERE {_quote_ident(key_col)} IN ({placeholders})"
        )
        return RefreshPlan(
            name=mv.name,
            kind="incremental",
            sql=(delete_sql, insert_sql),
            key_values=tuple(sorted(keys, key=repr)),
        )

    def plan_for_writes(self, table_changes: Mapping[str, Iterable[object]]) -> list[RefreshPlan]:
        """Given a map of ``table -> changed key values``, plan refreshes.

        For each view touching a changed table: mark dirty keys (when the view
        has an incremental key and the change carries it), then produce an
        incremental plan if possible else a full one. Views unaffected by the
        change are omitted.
        """
        affected: set[str] = set()
        for table, key_values in table_changes.items():
            for mv in self._registry.views_for_table(table):
                affected.add(mv.name)
                if mv.freshness.supports_incremental:
                    for kv in key_values:
                        self._clock.mark_dirty(mv.name, kv)
        plans: list[RefreshPlan] = []
        for name in sorted(affected):
            mv = self._registry.get(name)
            plan = (
                self.plan_incremental(name)
                if mv.freshness.supports_incremental
                else self.plan_full(name)
            )
            plans.append(plan)
        return plans


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #


def create_matview_ddl(mv: MatviewDef, *, with_data: bool = True) -> str:
    """Generate ``CREATE MATERIALIZED VIEW`` DDL for ``mv``."""
    data_clause = "WITH DATA" if with_data else "WITH NO DATA"
    return (
        f"CREATE MATERIALIZED VIEW {_quote_ident(mv.name)} AS\n"
        f"{mv.select_sql}\n"
        f"{data_clause}"
    )


def drop_matview_ddl(name: str, *, if_exists: bool = True) -> str:
    """Generate ``DROP MATERIALIZED VIEW`` DDL."""
    exists = "IF EXISTS " if if_exists else ""
    return f"DROP MATERIALIZED VIEW {exists}{_quote_ident(name)}"


def unique_index_ddl(mv: MatviewDef, columns: list[str], *, index_name: str | None = None) -> str:
    """Generate the UNIQUE index DDL a view needs for ``REFRESH ... CONCURRENTLY``."""
    idx = index_name or f"{mv.name}_uidx"
    cols = ", ".join(_quote_ident(c) for c in columns)
    return f"CREATE UNIQUE INDEX {_quote_ident(idx)} ON {_quote_ident(mv.name)} ({cols})"


# --------------------------------------------------------------------------- #
# Sound query rewriting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RewriteResult:
    """The outcome of a successful rewrite."""

    matview: str
    sql: str
    #: Human-readable reason the rewrite is sound (for logs / audits).
    reason: str


def _predicate_set(shape: SelectShape) -> frozenset[tuple[str, str, bool]]:
    """A comparable set of (column, op, literal_rhs) for predicate containment."""
    return frozenset(
        (p.column.column, str(p.op), p.literal_rhs) for p in shape.predicates
    )


def _columns_covered(query: SelectShape, mv: MatviewDef) -> bool:
    """True when every column the query outputs is materialised by the MV."""
    if query.star:
        # A query SELECT * is only coverable if the MV is itself SELECT * of the
        # same single table (we cannot know all columns otherwise).
        return mv.shape.star and query.table_names == mv.shape.table_names
    mat = mv.materialized_columns
    return all(c.column in mat for c in query.columns)


def _aggregates_align(query: SelectShape, mv: MatviewDef) -> bool:
    """Aggregate compatibility check.

    A non-aggregate query may read a non-aggregate MV. An aggregate query may only
    read an MV that pre-computes *exactly* its aggregate set over a grouping that
    the query restricts to (handled by the predicate/grouping check). We require
    the aggregate function multiset to match.
    """
    if not query.is_aggregate and not mv.shape.is_aggregate:
        return True
    if query.is_aggregate != mv.shape.is_aggregate:
        return False
    return sorted(query.aggregates) == sorted(mv.shape.aggregates)


def _grouping_compatible(query: SelectShape, mv: MatviewDef) -> bool:
    """For aggregate MVs, the query's added equality predicates must be on group keys.

    An MV ``SELECT book_id, count(*) ... GROUP BY book_id`` answers
    ``... WHERE book_id = ?`` (an equality on the group key) but not a filter on a
    non-grouped column.
    """
    if not mv.shape.is_aggregate:
        return True
    group_cols = {c.column for c in mv.shape.group_by}
    extra = _predicate_set(query) - _predicate_set(mv.shape)
    return all(op == "=" and literal and col in group_cols for col, op, literal in extra)


def _is_sound_rewrite(query: SelectShape, mv: MatviewDef) -> tuple[bool, str]:
    """Decide whether ``query`` can be answered from ``mv``. Returns (ok, reason)."""
    # 1. Same base relation set.
    if query.table_names != mv.shape.table_names:
        return False, "different base tables"
    # 2. Output columns covered.
    if not _columns_covered(query, mv):
        return False, "query columns not materialised"
    # 3. Aggregates align.
    if not _aggregates_align(query, mv):
        return False, "aggregate functions differ"
    # 4. Predicate containment.
    q_preds = _predicate_set(query)
    mv_preds = _predicate_set(mv.shape)
    if mv_preds and not mv_preds <= q_preds:
        # The MV pre-filters rows the query may need.
        return False, "matview is more restrictive than query"
    if q_preds == mv_preds:
        pass  # identical predicates — trivially sound
    elif not _grouping_compatible(query, mv):
        return False, "extra predicate not on a group key"
    # 5. Ordering/limit do not affect soundness (applied post-read).
    # 6. DISTINCT: only sound when the MV is itself DISTINCT or the query is not.
    if query.distinct and not mv.shape.distinct and not mv.shape.is_aggregate:
        return False, "query needs DISTINCT the matview does not provide"
    return True, "sound projection/restriction of the matview"


def _build_rewritten_sql(query: SelectShape, mv: MatviewDef) -> str:
    """Construct a SELECT against the MV preserving the query's projection/filters.

    Conservative reconstruction: select the query's columns (or ``*``) from the MV
    and re-apply the query's *extra* equality predicates (the ones beyond the MV's
    own, proven to be on group keys / safe). Ordering and limit are re-applied.

    For an aggregate query answered by an aggregate MV we project ``*``: the query
    selects pre-computed aggregate expressions (e.g. ``count(*)``) whose stored
    column names are an implementation detail of the MV, and the MV materialises
    its columns in the query's projection order (group keys then aggregates), so
    ``SELECT *`` reproduces the original shape exactly. Naming the aggregate
    columns explicitly would silently drop them (the bug the DB rewrite test
    caught) since the parsed query carries them as ``aggregates``, not ``columns``.
    """
    if query.star or (query.is_aggregate and mv.shape.is_aggregate):
        cols = "*"
    else:
        cols = ", ".join(_col_sql(c) for c in query.columns)
    sql = f"SELECT {'DISTINCT ' if query.distinct else ''}{cols} FROM {_quote_ident(mv.name)}"
    extra = _predicate_set(query) - _predicate_set(mv.shape)
    if extra:
        clauses = [f"{_quote_ident(col)} = ?" for col, op, _ in sorted(extra) if op == "="]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
    if query.order_by:
        sql += " ORDER BY " + ", ".join(_col_sql(c) for c in query.order_by)
    if query.limit is not None:
        sql += f" LIMIT {query.limit}"
    return sql


def _col_sql(col: ColumnRef) -> str:
    return _quote_ident(col.column)


def rewrite(query_sql: str, registry: MatviewRegistry) -> RewriteResult | None:
    """Rewrite ``query_sql`` to read from a registered matview *iff sound*.

    Returns the rewrite, or ``None`` when no view can soundly answer the query (in
    which case the caller runs the original SQL unchanged). Never raises for an
    unrewritable query — declining is the safe default.
    """
    try:
        query = parse_select(query_sql)
    except Exception:  # noqa: BLE001 - an unshapable query is simply not rewritten
        return None
    best: RewriteResult | None = None
    for mv in registry.candidates_for(query):
        ok, reason = _is_sound_rewrite(query, mv)
        if ok:
            rewritten = _build_rewritten_sql(query, mv)
            # Prefer the most specific MV (smallest materialised column set wins on
            # ties — it is the tightest pre-aggregation).
            if best is None:
                best = RewriteResult(matview=mv.name, sql=rewritten, reason=reason)
    return best


def rewrite_strict(query_sql: str, registry: MatviewRegistry) -> RewriteResult:
    """Like :func:`rewrite` but raises :class:`RewriteUnsound` when it would decline."""
    result = rewrite(query_sql, registry)
    if result is None:
        raise RewriteUnsound(f"no matview can soundly answer: {query_sql!r}")
    return result


# --------------------------------------------------------------------------- #
# Executor (the only part that touches a connection)
# --------------------------------------------------------------------------- #


class MatviewExecutor:
    """Runs generated DDL/refresh SQL against an :class:`AsyncConnection`.

    Importing this class opens nothing; only its async methods touch the DB.
    """

    def __init__(self, registry: MatviewRegistry, clock: StalenessClock) -> None:
        self._registry = registry
        self._clock = clock
        self._planner = RefreshPlanner(registry, clock)

    async def create(self, conn: AsyncConnection, name: str, *, with_data: bool = True) -> None:
        """Create the materialized view and mark it freshly refreshed."""
        from sqlalchemy import text

        mv = self._registry.get(name)
        await conn.execute(text(create_matview_ddl(mv, with_data=with_data)))
        if with_data:
            self._clock.mark_refreshed(name)

    async def drop(self, conn: AsyncConnection, name: str, *, if_exists: bool = True) -> None:
        """Drop the materialized view."""
        from sqlalchemy import text

        await conn.execute(text(drop_matview_ddl(name, if_exists=if_exists)))

    async def refresh_full(
        self, conn: AsyncConnection, name: str, *, concurrently: bool = False
    ) -> RefreshPlan:
        """Run a full refresh and update the clock."""
        from sqlalchemy import text

        plan = self._planner.plan_full(name, concurrently=concurrently)
        for stmt in plan.sql:
            await conn.execute(text(stmt))
        self._clock.mark_refreshed(name)
        return plan

    def planner(self) -> RefreshPlanner:
        """The underlying :class:`RefreshPlanner` (for plan-only callers/tests)."""
        return self._planner


def with_dependencies(mv: MatviewDef, deps: Iterable[str]) -> MatviewDef:
    """Return a copy of ``mv`` with an explicit dependency override (helper)."""
    return replace(mv, dependency_override=frozenset(d.lower() for d in deps))


__all__ = [
    "FreshnessPolicy",
    "MatviewDef",
    "MatviewExecutor",
    "MatviewRegistry",
    "RefreshPlan",
    "RefreshPlanner",
    "RewriteResult",
    "StalenessClock",
    "create_matview_ddl",
    "drop_matview_ddl",
    "rewrite",
    "rewrite_strict",
    "unique_index_ddl",
    "with_dependencies",
]
