"""The self-serve metrics service — the one public entrypoint.

:class:`SemanticLayer` composes every facet into a single ``query`` call that a
route, a notebook, or an assistant uses:

    layer = SemanticLayer(graph, engine, governance=..., cache=..., advisor=...)
    result = layer.query(metric_query, principal=who)

The call flow (each step delegates to a single-responsibility module):

1. **governance** — authorise the metrics + dimensions, derive the row filter +
   masked columns (raises :class:`AccessDenied` on a forbidden ask);
2. **compile** — lower the query (with the governance row filter conjoined) to a
   plan; record it with the materialization **advisor**;
3. **cache** — look the plan (scoped by the principal's access) up in the result
   cache; on a hit, return it;
4. **execute** — run the plan through the engine + post-aggregation;
5. **mask** — redact masked columns; cache the masked result; return.

Everything except the engine and a wall clock is pure, so the service is fully
unit-testable against the in-memory engine. Each collaborator is optional:
without a governance engine every ask is allowed; without a cache nothing is
memoised; without an advisor nothing is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.lakehouse.semantic.advisor import MaterializationAdvisor
from app.lakehouse.semantic.cache import ResultCache, cache_key, scope_token
from app.lakehouse.semantic.catalog import MetricsCatalog
from app.lakehouse.semantic.compiler import Compiler
from app.lakehouse.semantic.engine import QueryEngine
from app.lakehouse.semantic.executor import MetricResult, execute_plan
from app.lakehouse.semantic.governance import (
    GovernanceEngine,
    Principal,
)
from app.lakehouse.semantic.lineage import LineageGraph, MetricLineage
from app.lakehouse.semantic.plan import QueryPlan
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.types import and_all


@dataclass
class QueryOutcome:
    """A query result plus the observability the service can surface."""

    result: MetricResult
    plan: QueryPlan
    cache_hit: bool
    masked_dimensions: tuple[str, ...]


class SemanticLayer:
    """The composed self-serve metrics layer (the facade)."""

    def __init__(
        self,
        graph: SemanticGraph,
        engine: QueryEngine,
        *,
        governance: GovernanceEngine | None = None,
        cache: ResultCache | None = None,
        advisor: MaterializationAdvisor | None = None,
        catalog_tags: dict[str, tuple[str, ...]] | None = None,
    ):
        self.graph = graph
        self.engine = engine
        self.governance = governance
        self.cache = cache
        self.advisor = advisor
        self._compiler = Compiler(graph)
        self._lineage = LineageGraph(graph)
        self.catalog = MetricsCatalog(graph, tags=catalog_tags or {})

    # -- the main call ----------------------------------------------------- #

    def query(
        self, request: MetricQuery, *, principal: Principal | None = None
    ) -> QueryOutcome:
        masked: frozenset[str] = frozenset()
        gov_filter = None
        if self.governance is not None:
            if principal is None:
                raise ValueError("governance is configured but no principal was supplied")
            governed = self.governance.authorize(
                principal,
                requested_metrics=request.metrics,
                requested_dimensions=request.group_by,
            )
            gov_filter = governed.row_filter
            masked = governed.masked_dimensions

        # Conjoin the governance row filter onto the user's filters.
        effective = request
        if gov_filter is not None:
            combined = and_all(*request.filters, gov_filter)
            new_filters = (combined,) if combined is not None else ()
            effective = MetricQuery(
                metrics=request.metrics,
                group_by=request.group_by,
                time_grain=request.time_grain,
                time_dimension=request.time_dimension,
                time_window=request.time_window,
                filters=new_filters,
                order_by=request.order_by,
                limit=request.limit,
            )

        plan = self._compiler.compile(effective)
        if self.advisor is not None:
            self.advisor.observe(plan)

        scope = scope_token(repr(gov_filter) if gov_filter else None, masked)
        key = cache_key(plan, scope)

        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return QueryOutcome(
                    result=cached,
                    plan=plan,
                    cache_hit=True,
                    masked_dimensions=tuple(sorted(masked)),
                )

        result = execute_plan(plan, self.engine)
        if masked:
            rows = GovernanceEngine.apply_masking([dict(r) for r in result.rows], masked)
            result = MetricResult(
                dimensions=result.dimensions,
                time_column=result.time_column,
                metrics=result.metrics,
                rows=tuple(rows),
            )
        if self.cache is not None:
            self.cache.put(key, result)
        return QueryOutcome(
            result=result,
            plan=plan,
            cache_hit=False,
            masked_dimensions=tuple(sorted(masked)),
        )

    # -- discovery / introspection ---------------------------------------- #

    def compile(self, request: MetricQuery) -> QueryPlan:
        """Compile without executing (preview the plan / fingerprint)."""
        return self._compiler.compile(request)

    def lineage(self, metric: str) -> MetricLineage:
        return self._lineage.lineage_of(metric)

    def recommendations(self) -> tuple:
        return self.advisor.recommendations() if self.advisor else ()


__all__ = ["QueryOutcome", "SemanticLayer"]
