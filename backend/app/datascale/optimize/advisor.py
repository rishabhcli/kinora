"""Index advisor — recommend (and what-if) indexes from a query workload.

Given a *workload* (a set of query shapes with execution weights), the advisor:

1. **Generates candidate indexes** from each query's structure (via
   :mod:`sqlshape`): equality predicate columns first (the seek prefix), then a
   range column, then ORDER BY columns (so the index can also satisfy the sort),
   plus single-column indexes for join keys. Equality columns are ordered for a
   stable, mergeable key.
2. **Estimates benefit** with a transparent cost model: a query that currently
   does a sequential scan of an estimated-size table is charged ``rows`` work; a
   matching index turns equality seeks into ``log2(rows)`` and a covering sort
   removes a separate sort cost. The *benefit* of a candidate for a query is
   ``(seq_cost - index_cost) * weight``. When ``hypopg`` is available, a real
   what-if can replace the model (see :func:`whatif_with_hypopg`), but the model
   stands alone so the advisor needs no extension or live DB.
3. **Ranks + prunes.** Candidates are summed across the workload, then redundant
   ones are pruned: an index whose key is a *prefix* of another recommended
   index's key is dropped (the longer one serves both), and exact duplicates are
   merged. The result is a minimal, ranked recommendation set with the DDL to
   create each.

Everything is pure and deterministic; the table-size model is injectable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.datascale.optimize.sqlshape import SelectShape, try_parse_select

# --------------------------------------------------------------------------- #
# Workload
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkloadQuery:
    """One query shape in a workload, with an execution weight (calls)."""

    sql: str
    weight: float = 1.0


@dataclass(slots=True)
class Workload:
    """A collection of weighted query shapes the advisor analyses."""

    queries: list[WorkloadQuery] = field(default_factory=list)

    def add(self, sql: str, weight: float = 1.0) -> None:
        """Append a query shape to the workload."""
        self.queries.append(WorkloadQuery(sql=sql, weight=weight))

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, float]]) -> Workload:
        """Build a workload from ``(sql, weight)`` pairs."""
        return cls(queries=[WorkloadQuery(sql=s, weight=w) for s, w in pairs])

    def shapes(self) -> list[tuple[SelectShape, float]]:
        """Parsed shapes paired with weights (unshapable queries are skipped)."""
        out: list[tuple[SelectShape, float]] = []
        for q in self.queries:
            shape = try_parse_select(q.sql)
            if shape is not None:
                out.append((shape, q.weight))
        return out


# --------------------------------------------------------------------------- #
# Candidate indexes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IndexCandidate:
    """A candidate index: a table + an ordered tuple of key columns."""

    table: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise ValueError("an index needs at least one column")

    @property
    def name(self) -> str:
        """A deterministic index name (``ix_<table>_<col>_<col>``)."""
        return f"ix_{self.table}_" + "_".join(self.columns)

    def ddl(self, *, concurrently: bool = True, unique: bool = False) -> str:
        """``CREATE INDEX`` DDL for this candidate."""
        conc = "CONCURRENTLY " if concurrently else ""
        uniq = "UNIQUE " if unique else ""
        cols = ", ".join(f'"{c}"' for c in self.columns)
        return f'CREATE {uniq}INDEX {conc}IF NOT EXISTS "{self.name}" ON "{self.table}" ({cols})'

    def is_prefix_of(self, other: IndexCandidate) -> bool:
        """True when this index's key is a proper prefix of ``other``'s (same table)."""
        if self.table != other.table:
            return False
        if len(self.columns) >= len(other.columns):
            return False
        return other.columns[: len(self.columns)] == self.columns

    def covers(self, other: IndexCandidate) -> bool:
        """True when this index serves every query ``other`` would (prefix or equal)."""
        return self == other or other.is_prefix_of(self)


def candidates_for_shape(shape: SelectShape) -> list[IndexCandidate]:
    """Generate candidate indexes for one query shape.

    Strategy per table referenced:

    * A composite of ``[equality cols (sorted), one range col, order-by cols]`` —
      the textbook "equality, then range, then sort" key order.
    * A single-column index per join key column belonging to that table.

    Columns are resolved to their owning table when qualified; unqualified columns
    are attributed to the sole table in a single-table query (and skipped in a
    multi-table query, where we cannot know the owner).
    """
    candidates: list[IndexCandidate] = []
    table_names = list(shape.table_names)
    single_table = table_names[0] if len(table_names) == 1 else None

    def owner(col_table: str | None) -> str | None:
        if col_table is not None:
            # Map an alias back to its base table.
            for t in shape.tables:
                if t.key == col_table:
                    return t.name
            return col_table
        return single_table

    # Group seek columns per table.
    per_table_eq: dict[str, list[str]] = {}
    per_table_range: dict[str, list[str]] = {}
    per_table_order: dict[str, list[str]] = {}

    for col in shape.equality_columns():
        t = owner(col.table)
        if t:
            per_table_eq.setdefault(t, [])
            if col.column not in per_table_eq[t]:
                per_table_eq[t].append(col.column)
    for col in shape.range_columns():
        t = owner(col.table)
        if t:
            per_table_range.setdefault(t, []).append(col.column)
    for col in shape.order_by:
        t = owner(col.table)
        if t:
            per_table_order.setdefault(t, []).append(col.column)

    tables_seen = set(per_table_eq) | set(per_table_range) | set(per_table_order)
    for table in sorted(tables_seen):
        cols: list[str] = []
        # Equality columns sorted for a stable, mergeable prefix.
        cols.extend(sorted(per_table_eq.get(table, [])))
        # One range column (a btree uses only the first range col effectively).
        rng = per_table_range.get(table, [])
        if rng:
            cols.append(rng[0])
        # ORDER BY columns extend the key only when there is no range col (a range
        # predicate already breaks sort-ordering usefulness past the range col).
        if not rng:
            for oc in per_table_order.get(table, []):
                if oc not in cols:
                    cols.append(oc)
        # De-dup while preserving order.
        ordered = list(dict.fromkeys(cols))
        if ordered:
            candidates.append(IndexCandidate(table=table, columns=tuple(ordered)))

    # Join-key single-column indexes.
    for jc in shape.joins:
        for ref in (jc.left, jc.right):
            t = owner(ref.table)
            if t:
                candidates.append(IndexCandidate(table=t, columns=(ref.column,)))

    return candidates


# --------------------------------------------------------------------------- #
# Cost model + benefit
# --------------------------------------------------------------------------- #

#: Default assumed table size when the caller gives no estimate.
DEFAULT_TABLE_ROWS = 100_000

TableSizes = Mapping[str, int]


def _table_rows(table: str, sizes: TableSizes | None) -> float:
    if sizes and table in sizes:
        return float(max(1, sizes[table]))
    return float(DEFAULT_TABLE_ROWS)


def _seq_cost(shape: SelectShape, sizes: TableSizes | None) -> float:
    """Model the cost of the current plan (a full scan of the largest table)."""
    return max((_table_rows(t, sizes) for t in shape.table_names), default=1.0)


def _index_cost(
    shape: SelectShape, candidate: IndexCandidate, sizes: TableSizes | None
) -> float | None:
    """Model the cost of ``shape`` *using* ``candidate``; ``None`` if it cannot.

    A useful index must lead with at least one equality column the query
    constrains. The cost is logarithmic in the table size for the seek, plus a
    small per-matched-row factor; a covering ORDER BY removes a sort term.
    """
    rows = _table_rows(candidate.table, sizes)
    eq_cols = {c.column for c in shape.equality_columns()}
    rng_cols = {c.column for c in shape.range_columns()}
    lead = candidate.columns[0]
    if lead not in eq_cols and lead not in rng_cols:
        return None  # the index cannot serve this query's predicates
    # Seek cost: log of the table, then a fan-out proportional to unmatched key
    # columns (fewer matched key columns → more rows scanned within the index).
    matched = sum(1 for c in candidate.columns if c in eq_cols or c in rng_cols)
    selectivity = 0.5 ** matched  # each matched key column halves the result set
    seek = math.log2(rows + 1.0)
    scan = rows * selectivity
    cost = seek + scan
    # Reward an index that also satisfies the query's sort order: after the
    # equality-matched key prefix, the remaining key columns must lead with the
    # ORDER BY columns for the index to provide the sort for free.
    order_cols = tuple(c.column for c in shape.order_by)
    if order_cols:
        eq_prefix_len = len(eq_cols & set(candidate.columns))
        sort_suffix = candidate.columns[eq_prefix_len:][: len(order_cols)]
        if sort_suffix == order_cols:
            cost -= rows * 0.05  # saved an explicit sort
    return max(cost, 1.0)


@dataclass(slots=True)
class IndexRecommendation:
    """A recommended index with its workload-summed benefit + supporting queries."""

    candidate: IndexCandidate
    benefit: float
    supporting_queries: int

    @property
    def ddl(self) -> str:
        return self.candidate.ddl()

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.candidate.name,
            "table": self.candidate.table,
            "columns": list(self.candidate.columns),
            "benefit": round(self.benefit, 3),
            "supporting_queries": self.supporting_queries,
            "ddl": self.ddl,
        }


class IndexAdvisor:
    """Recommends a minimal, ranked set of indexes for a workload."""

    def __init__(self, *, table_sizes: TableSizes | None = None) -> None:
        self._sizes = table_sizes

    def candidates(self, workload: Workload) -> list[IndexCandidate]:
        """All distinct candidate indexes the workload suggests (unranked)."""
        seen: dict[tuple[str, tuple[str, ...]], IndexCandidate] = {}
        for shape, _w in workload.shapes():
            for cand in candidates_for_shape(shape):
                seen[(cand.table, cand.columns)] = cand
        return [seen[k] for k in sorted(seen)]

    def recommend(
        self, workload: Workload, *, min_benefit: float = 0.0
    ) -> list[IndexRecommendation]:
        """Rank candidates by total benefit, prune redundant prefixes, return DDL.

        ``min_benefit`` filters out candidates whose summed benefit does not clear
        the bar (an index that barely helps is not worth its write cost).
        """
        shapes = workload.shapes()
        # 1. Score each distinct candidate across the whole workload.
        scores: dict[IndexCandidate, float] = {}
        support: dict[IndexCandidate, int] = {}
        for cand in self.candidates(workload):
            total = 0.0
            n = 0
            for shape, weight in shapes:
                if cand.table not in shape.table_names:
                    continue
                idx_cost = _index_cost(shape, cand, self._sizes)
                if idx_cost is None:
                    continue
                benefit = (_seq_cost(shape, self._sizes) - idx_cost) * weight
                if benefit > 0:
                    total += benefit
                    n += 1
            if total > min_benefit and n > 0:
                scores[cand] = total
                support[cand] = n
        # 2. Prune redundant prefixes: drop a candidate whose key is a prefix of
        #    another *kept* candidate (the longer one serves both).
        kept = self._prune_redundant(list(scores))
        recs = [
            IndexRecommendation(candidate=c, benefit=scores[c], supporting_queries=support[c])
            for c in kept
        ]
        recs.sort(key=lambda r: r.benefit, reverse=True)
        return recs

    @staticmethod
    def _prune_redundant(candidates: list[IndexCandidate]) -> list[IndexCandidate]:
        """Remove any candidate that is a prefix of another candidate (same table)."""
        kept: list[IndexCandidate] = []
        for cand in candidates:
            if any(cand.is_prefix_of(other) for other in candidates if other is not cand):
                continue
            kept.append(cand)
        # Drop exact duplicates (defensive; candidates() already de-dups).
        unique: list[IndexCandidate] = []
        seen: set[IndexCandidate] = set()
        for c in kept:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique


def whatif_with_hypopg() -> bool:
    """Report whether the optional ``hypopg`` what-if path could be used.

    The pure cost model is the default and needs no extension. A live what-if via
    the ``hypopg`` Postgres extension (``hypopg_create_index`` + ``EXPLAIN``) is a
    future integration; this stub lets callers branch without importing anything
    heavy. It returns ``False`` here (the model is authoritative for tests).
    """
    return False


__all__ = [
    "DEFAULT_TABLE_ROWS",
    "IndexAdvisor",
    "IndexCandidate",
    "IndexRecommendation",
    "Workload",
    "WorkloadQuery",
    "candidates_for_shape",
    "whatif_with_hypopg",
]
