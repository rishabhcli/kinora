"""Materialization-advisor + result-cache tests (deterministic, fake clock)."""

from __future__ import annotations

from app.lakehouse.semantic.advisor import (
    AggregationShape,
    MaterializationAdvisor,
)
from app.lakehouse.semantic.cache import (
    InMemoryResultCache,
    cache_key,
    scope_token,
)
from app.lakehouse.semantic.compiler import compile_query
from app.lakehouse.semantic.executor import MetricResult
from app.lakehouse.semantic.kpis import kpi_metrics
from app.lakehouse.semantic.plan import QueryPlan
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.types import TimeGrain
from tests.lakehouse_fixtures import books_model, shots_model


def _graph() -> SemanticGraph:
    return SemanticGraph.build([shots_model(), books_model()], list(kpi_metrics()))


def _plan(query: MetricQuery) -> QueryPlan:
    return compile_query(_graph(), query)


# --------------------------------------------------------------------------- #
# Advisor
# --------------------------------------------------------------------------- #


def test_advisor_recommends_hot_shape() -> None:
    advisor = MaterializationAdvisor(min_frequency=3)
    plan = _plan(MetricQuery.of("total_video_seconds", group_by=("agent_role",)))
    for _ in range(5):
        advisor.observe(plan)
    recs = advisor.recommendations()
    assert len(recs) == 1
    assert recs[0].frequency == 5
    assert recs[0].shape.base_model == "shots"
    assert recs[0].shape.dimensions == ("agent_role",)
    assert recs[0].shape.additive is True


def test_advisor_below_threshold_no_recommendation() -> None:
    advisor = MaterializationAdvisor(min_frequency=10)
    plan = _plan(MetricQuery.of("total_video_seconds"))
    advisor.observe(plan)
    assert advisor.recommendations() == ()


def test_advisor_coverage_rollup_serves_subset() -> None:
    advisor = MaterializationAdvisor(min_frequency=1)
    # A wide rollup: group by agent_role + book_id at DAY.
    wide = AggregationShape(
        base_model="shots",
        dimensions=("agent_role", "book_id"),
        grain=TimeGrain.DAY,
        measures=("shots.total_seconds", "shots.shot_count"),
        additive=True,
    )
    # A narrower query: group by just agent_role at MONTH over one measure.
    narrow = AggregationShape(
        base_model="shots",
        dimensions=("agent_role",),
        grain=TimeGrain.MONTH,
        measures=("shots.total_seconds",),
        additive=True,
    )
    assert wide.covers(narrow)
    advisor.observe_shape(narrow, count=4)
    coverage = advisor.coverage((wide,))
    assert coverage[wide] == 4


def test_advisor_finer_grain_cannot_serve_coarser_only() -> None:
    # A MONTH rollup cannot serve a DAY query (would need finer data).
    month_rollup = AggregationShape(
        base_model="shots",
        dimensions=(),
        grain=TimeGrain.MONTH,
        measures=("shots.total_seconds",),
        additive=True,
    )
    day_query = AggregationShape(
        base_model="shots",
        dimensions=(),
        grain=TimeGrain.DAY,
        measures=("shots.total_seconds",),
        additive=True,
    )
    assert not month_rollup.covers(day_query)


def test_advisor_best_single_materialization() -> None:
    advisor = MaterializationAdvisor(min_frequency=1)
    advisor.observe(_plan(MetricQuery.of("total_video_seconds", group_by=("agent_role",))))
    advisor.observe(_plan(MetricQuery.of("total_video_seconds", group_by=("agent_role",))))
    advisor.observe(_plan(MetricQuery.of("shot_total", group_by=("book_id",))))
    best = advisor.best_single_materialization()
    assert best is not None
    assert best.shape.base_model == "shots"


def test_non_additive_shape_not_coarse_reusable() -> None:
    # COUNT_DISTINCT book_count is non-additive; its shape can't roll up.
    from app.lakehouse.semantic.metrics import SimpleMetric

    graph = SemanticGraph.build(
        [books_model()],
        [SimpleMetric(name="unique_books", measure="book_count")],
    )
    plan = compile_query(graph, MetricQuery.of("unique_books", group_by=("genre",)))
    shape = AggregationShape.of(plan)
    assert shape.additive is False
    # Even an identical shape can't be "covered" for re-aggregation.
    assert not shape.covers(shape)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def _empty_result() -> MetricResult:
    return MetricResult(dimensions=(), time_column=None, metrics=("m",), rows=())


def test_cache_get_put_hit_miss() -> None:
    cache = InMemoryResultCache(ttl_seconds=100)
    assert cache.get("k") is None
    cache.put("k", _empty_result())
    assert cache.get("k") is not None
    assert cache.stats.hits == 1
    assert cache.stats.misses == 1


def test_cache_ttl_expiry() -> None:
    clock = _FakeClock()
    cache = InMemoryResultCache(ttl_seconds=10, clock=clock)
    cache.put("k", _empty_result())
    clock.t = 5
    assert cache.get("k") is not None
    clock.t = 11
    assert cache.get("k") is None
    assert cache.stats.expirations == 1


def test_cache_lru_eviction() -> None:
    cache = InMemoryResultCache(max_entries=2, ttl_seconds=1000)
    cache.put("a", _empty_result())
    cache.put("b", _empty_result())
    cache.get("a")  # touch a so b is LRU
    cache.put("c", _empty_result())  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.stats.evictions == 1


def test_cache_invalidate_and_clear() -> None:
    cache = InMemoryResultCache()
    cache.put("k", _empty_result())
    assert cache.invalidate("k") is True
    assert cache.invalidate("k") is False
    cache.put("x", _empty_result())
    cache.clear()
    assert len(cache) == 0


def test_cache_key_folds_scope() -> None:
    plan = _plan(MetricQuery.of("shot_total"))
    k1 = cache_key(plan, scope_token("filter_a", frozenset()))
    k2 = cache_key(plan, scope_token("filter_b", frozenset()))
    assert k1 != k2
    # Same plan, same scope -> identical key (deterministic).
    assert cache_key(plan) == cache_key(plan)


def test_cache_hit_rate() -> None:
    cache = InMemoryResultCache()
    cache.put("k", _empty_result())
    cache.get("k")
    cache.get("missing")
    assert cache.stats.lookups == 2
    assert cache.stats.hit_rate == 0.5
