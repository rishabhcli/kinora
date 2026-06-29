"""Query optimization platform (facet B of database-at-scale).

A self-contained performance platform the app's hot paths can adopt:

* **fingerprint / sqlshape** — SQL normalisation + structural shaping, the shared
  identity and structure primitives every other layer reasons over.
* **matview** — automatic materialized views: typed definitions, full +
  incremental refresh with a staleness clock, and *sound* transparent
  query→matview rewriting (rewrites only when provably equivalent).
* **resultcache** — a query-result cache with precise dependency-based
  invalidation (write to table T invalidates only entries that read T).
* **nplusone** — an N+1 detector + an async dataloader/batch-resolver framework.
* **profiler** — a hot-path profiler aggregating query stats into flamegraph-style
  reports, consuming :mod:`app.db.inspect`'s EXPLAIN output.
* **advisor** — an index advisor: candidate generation + what-if costing →
  ranked, redundancy-pruned recommendations from a workload.
* **regression** — a plan-regression guard comparing fresh plans to a baseline.
* **workloadgen** — a seedable synthetic workload generator for deterministic
  tests of every layer.
* **platform** — an opt-in :class:`OptimizePlatform` facade wiring it together.

Importing any module here opens no sockets and needs no network. See ``DESIGN.md``
for the soundness model and the phase roadmap.
"""

from __future__ import annotations

from app.datascale.optimize.advisor import (
    IndexAdvisor,
    IndexCandidate,
    IndexRecommendation,
    Workload,
    WorkloadQuery,
    candidates_for_shape,
)
from app.datascale.optimize.errors import (
    CacheError,
    OptimizeError,
    ParseError,
    RefreshError,
    RegressionDetected,
    RewriteUnsound,
    UnknownMatview,
)
from app.datascale.optimize.fingerprint import (
    QueryFingerprint,
    fingerprint,
    make_fingerprint,
    normalize_sql,
    referenced_tables,
)
from app.datascale.optimize.matview import (
    FreshnessPolicy,
    MatviewDef,
    MatviewExecutor,
    MatviewRegistry,
    RefreshPlan,
    RefreshPlanner,
    RewriteResult,
    StalenessClock,
    create_matview_ddl,
    rewrite,
    rewrite_strict,
)
from app.datascale.optimize.nplusone import (
    DataLoader,
    DataLoaderStats,
    NPlusOneDetector,
    NPlusOneFinding,
    Severity,
)
from app.datascale.optimize.platform import ObserveResult, OptimizePlatform
from app.datascale.optimize.profiler import (
    FlameGraph,
    HotPathReport,
    QueryProfiler,
    ShapeStat,
)
from app.datascale.optimize.regression import (
    BaselineStore,
    GuardReport,
    PlanDiff,
    PlanRegressionGuard,
    PlanSnapshot,
    compare_plans,
    snapshot_from_plan,
)
from app.datascale.optimize.resultcache import (
    CacheStats,
    ResultCache,
    RowScope,
    make_cache_key,
)
from app.datascale.optimize.sqlshape import (
    ColumnRef,
    JoinCondition,
    Predicate,
    PredicateOp,
    SelectShape,
    TableRef,
    parse_select,
    try_parse_select,
)
from app.datascale.optimize.workloadgen import (
    GeneratedQuery,
    QueryKind,
    WorkloadGenerator,
    WorkloadSpec,
)

__all__ = [
    "BaselineStore",
    "CacheError",
    "CacheStats",
    "ColumnRef",
    "DataLoader",
    "DataLoaderStats",
    "FlameGraph",
    "FreshnessPolicy",
    "GeneratedQuery",
    "GuardReport",
    "HotPathReport",
    "IndexAdvisor",
    "IndexCandidate",
    "IndexRecommendation",
    "JoinCondition",
    "MatviewDef",
    "MatviewExecutor",
    "MatviewRegistry",
    "NPlusOneDetector",
    "NPlusOneFinding",
    "ObserveResult",
    "OptimizeError",
    "OptimizePlatform",
    "ParseError",
    "PlanDiff",
    "PlanRegressionGuard",
    "PlanSnapshot",
    "Predicate",
    "PredicateOp",
    "QueryFingerprint",
    "QueryKind",
    "QueryProfiler",
    "RefreshError",
    "RefreshPlan",
    "RefreshPlanner",
    "RegressionDetected",
    "ResultCache",
    "RewriteResult",
    "RewriteUnsound",
    "RowScope",
    "SelectShape",
    "Severity",
    "ShapeStat",
    "StalenessClock",
    "TableRef",
    "UnknownMatview",
    "Workload",
    "WorkloadGenerator",
    "WorkloadQuery",
    "WorkloadSpec",
    "candidates_for_shape",
    "compare_plans",
    "create_matview_ddl",
    "fingerprint",
    "make_cache_key",
    "make_fingerprint",
    "normalize_sql",
    "parse_select",
    "referenced_tables",
    "rewrite",
    "rewrite_strict",
    "snapshot_from_plan",
    "try_parse_select",
]
