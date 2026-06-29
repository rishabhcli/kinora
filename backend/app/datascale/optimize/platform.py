"""``OptimizePlatform`` — the opt-in facade wiring the layers together.

Nothing in this package is constructed implicitly; a caller that wants the whole
platform builds one :class:`OptimizePlatform`, which owns:

* a :class:`~app.datascale.optimize.matview.MatviewRegistry` + clock + planner,
* a :class:`~app.datascale.optimize.resultcache.ResultCache`,
* a :class:`~app.datascale.optimize.profiler.QueryProfiler`,
* a per-window :class:`~app.datascale.optimize.nplusone.NPlusOneDetector`,
* a :class:`~app.datascale.optimize.advisor.IndexAdvisor`,
* a :class:`~app.datascale.optimize.regression.PlanRegressionGuard` over a
  :class:`~app.datascale.optimize.regression.BaselineStore`.

The single hot-path entrypoint is :meth:`observe`: feed it every executed query
(sql, latency, rows, params, dependency set) and it updates the profiler, the
detector, and — when a result is supplied — the cache, applying the rewrite
discipline first so a query that a matview can soundly answer is recorded against
the matview. A write is announced via :meth:`on_write`, which invalidates the
cache precisely and marks the affected matviews dirty.

This is intentionally side-effect-free at import and holds no DB handle: it is the
*analysis + policy* plane. Executing rewrites / refreshes against a connection is
the caller's job (using :class:`~app.datascale.optimize.matview.MatviewExecutor`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.datascale.optimize.advisor import IndexAdvisor, Workload
from app.datascale.optimize.matview import (
    MatviewDef,
    MatviewRegistry,
    RefreshPlan,
    RefreshPlanner,
    RewriteResult,
    StalenessClock,
    rewrite,
)
from app.datascale.optimize.nplusone import NPlusOneDetector, NPlusOneFinding
from app.datascale.optimize.profiler import HotPathReport, QueryProfiler
from app.datascale.optimize.regression import BaselineStore, PlanRegressionGuard
from app.datascale.optimize.resultcache import ResultCache, RowScope

V = TypeVar("V")


@dataclass(slots=True)
class ObserveResult(Generic[V]):
    """What :meth:`OptimizePlatform.observe` resolved for one query."""

    #: The matview rewrite that applies, if any (the query is answerable from an MV).
    rewrite: RewriteResult | None
    #: The cached value, when this observation was a cache hit.
    cached: V | None
    #: True when the value came from the cache rather than the supplied result.
    cache_hit: bool


class OptimizePlatform(Generic[V]):
    """One opt-in object wiring every optimization layer behind a small API."""

    def __init__(
        self,
        *,
        cache_capacity: int = 4096,
        cache_default_ttl_s: float | None = None,
        n_plus_one_threshold: int = 5,
        cost_tolerance: float = 1.5,
        table_sizes: dict[str, int] | None = None,
        baseline_store: BaselineStore | None = None,
    ) -> None:
        self.matviews = MatviewRegistry()
        self.clock = StalenessClock()
        self.refresh_planner = RefreshPlanner(self.matviews, self.clock)
        self.cache: ResultCache[V] = ResultCache(
            capacity=cache_capacity, default_ttl_s=cache_default_ttl_s
        )
        self.profiler = QueryProfiler()
        self.detector = NPlusOneDetector(threshold=n_plus_one_threshold)
        self.advisor = IndexAdvisor(table_sizes=table_sizes)
        self.baselines = baseline_store or BaselineStore()
        self.regression_guard = PlanRegressionGuard(
            self.baselines, cost_tolerance=cost_tolerance
        )

    # ---- matview registration ---- #

    def register_matview(self, mv: MatviewDef) -> None:
        """Register a materialized view for transparent rewrite + refresh tracking."""
        self.matviews.register(mv)

    def try_rewrite(self, sql: str) -> RewriteResult | None:
        """Return a sound matview rewrite for ``sql`` or ``None`` (declines safely)."""
        return rewrite(sql, self.matviews)

    # ---- the hot-path entrypoint ---- #

    def observe(
        self,
        sql: str,
        *,
        latency_ms: float = 0.0,
        rows: int = 0,
        params: Any = None,
        result: V | None = None,
        dependencies: Iterable[str] | None = None,
        row_scopes: Iterable[RowScope] | None = None,
        stack: list[str] | None = None,
        cacheable: bool = False,
    ) -> ObserveResult[V]:
        """Record one executed query across the profiler, detector, and cache.

        When ``cacheable`` and a ``result`` are given the value is cached with its
        dependency set (auto-derived when ``dependencies`` is omitted). A prior
        cached value short-circuits as a hit. A matview rewrite, when one applies,
        is reported (the caller decides whether to route the next read through it).
        """
        self.profiler.record(sql, latency_ms, rows=rows, stack=stack)
        self.detector.observe(sql, params=params)

        # Cache lookup first (a hit avoids re-recording the result).
        if cacheable:
            cached = self.cache.get(sql, params)
            if cached is not None:
                return ObserveResult(rewrite=self.try_rewrite(sql), cached=cached, cache_hit=True)

        rw = self.try_rewrite(sql)

        if cacheable and result is not None:
            self.cache.put(
                sql,
                result,
                params=params,
                dependencies=dependencies,
                row_scopes=row_scopes,
            )
        return ObserveResult(rewrite=rw, cached=result if cacheable else None, cache_hit=False)

    # ---- writes ---- #

    def on_write(
        self,
        table: str,
        *,
        row_scopes: Iterable[RowScope] | None = None,
        changed_keys: Iterable[object] | None = None,
    ) -> list[RefreshPlan]:
        """Announce a write: invalidate the cache + plan affected matview refreshes.

        Returns the matview refresh plans the caller should execute. The result
        cache is invalidated precisely (row-scoped when scopes are given).
        """
        self.cache.invalidate_write(table, row_scopes=row_scopes)
        keys = list(changed_keys) if changed_keys is not None else []
        return self.refresh_planner.plan_for_writes({table: keys})

    # ---- reporting ---- #

    def hot_paths(self) -> HotPathReport:
        """The profiler's ranked hot-path report."""
        return self.profiler.report()

    def n_plus_one_findings(self) -> list[NPlusOneFinding]:
        """Current N+1 findings for the open window."""
        return self.detector.findings()

    def recommend_indexes(self, workload: Workload | None = None) -> list[dict[str, object]]:
        """Index recommendations from a supplied workload (or the profiled shapes).

        When ``workload`` is omitted, one is synthesised from the profiler's
        observed shapes (each shape weighted by its call count) so the platform can
        advise purely from what it has seen.
        """
        wl = workload if workload is not None else self._workload_from_profiler()
        return [r.as_dict() for r in self.advisor.recommend(wl)]

    def _workload_from_profiler(self) -> Workload:
        wl = Workload()
        for shape in self.profiler.report().shapes:
            wl.add(shape.skeleton, weight=float(shape.calls))
        return wl

    def begin_window(self) -> None:
        """Reset the per-window N+1 detector (call at request/job start)."""
        self.detector.reset()

    def snapshot_stats(self) -> dict[str, Any]:
        """A combined metrics view for the §12.5 observability panel."""
        return {
            "cache": self.cache.stats.as_dict(),
            "hot_paths": self.hot_paths().as_dict(),
            "n_plus_one": [f.as_dict() for f in self.n_plus_one_findings()],
            "matviews": len(self.matviews),
        }


__all__ = ["ObserveResult", "OptimizePlatform"]
