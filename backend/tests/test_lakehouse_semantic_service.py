"""Service-facade tests: governance + cache + advisor wired end-to-end.

Exercises the full self-serve path through :class:`SemanticLayer`: row-level +
column-level governance, result caching (incl. per-principal scope isolation),
and that the advisor records the compiled plans.
"""

from __future__ import annotations

import math

import pytest

from app.lakehouse.semantic.advisor import MaterializationAdvisor
from app.lakehouse.semantic.cache import InMemoryResultCache
from app.lakehouse.semantic.governance import (
    AccessDenied,
    AccessPolicy,
    ColumnAction,
    GovernanceEngine,
    Principal,
    StaticPolicyStore,
)
from app.lakehouse.semantic.kpis import buffer_kpi_metrics, kpi_metrics
from app.lakehouse.semantic.query import MetricQuery
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.service import SemanticLayer
from app.lakehouse.semantic.types import Comparison, FieldRef, Predicate
from tests.lakehouse_fixtures import (
    books_model,
    buffer_model,
    make_engine,
    shots_model,
)


def _graph() -> SemanticGraph:
    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


# --------------------------------------------------------------------------- #
# No governance / cache (defaults)
# --------------------------------------------------------------------------- #


def test_query_without_governance_runs() -> None:
    layer = SemanticLayer(_graph(), make_engine())
    out = layer.query(MetricQuery.of("accepted_footage_efficiency"))
    assert math.isclose(out.result.rows[0]["accepted_footage_efficiency"], 75.0)
    assert out.cache_hit is False


# --------------------------------------------------------------------------- #
# Caching + scope isolation
# --------------------------------------------------------------------------- #


def test_cache_hit_on_repeat() -> None:
    cache = InMemoryResultCache(ttl_seconds=1000)
    layer = SemanticLayer(_graph(), make_engine(), cache=cache)
    q = MetricQuery.of("shot_total", group_by=("agent_role",))
    first = layer.query(q)
    second = layer.query(q)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert cache.stats.hits == 1
    assert second.result.rows == first.result.rows


def test_cache_scope_isolation_between_principals() -> None:
    # Two principals with different row filters must NOT share a cache entry.
    store = StaticPolicyStore(
        by_role={
            "book_a_only": AccessPolicy(
                row_filter=Predicate(
                    field=FieldRef(name="book_id"), op=Comparison.EQ, value="book_a"
                )
            ),
            "book_b_only": AccessPolicy(
                row_filter=Predicate(
                    field=FieldRef(name="book_id"), op=Comparison.EQ, value="book_b"
                )
            ),
        }
    )
    cache = InMemoryResultCache(ttl_seconds=1000)
    layer = SemanticLayer(
        _graph(),
        make_engine(),
        governance=GovernanceEngine(store),
        cache=cache,
    )
    alice = Principal(subject="alice", roles=frozenset({"book_a_only"}))
    bob = Principal(subject="bob", roles=frozenset({"book_b_only"}))
    q = MetricQuery.of("shot_total")
    a = layer.query(q, principal=alice)
    b = layer.query(q, principal=bob)
    # Both saw 4 shots (their own book) but neither is a cache hit of the other.
    assert a.result.rows[0]["shot_total"] == 4
    assert b.result.rows[0]["shot_total"] == 4
    assert a.cache_hit is False and b.cache_hit is False
    # Re-running alice IS a hit.
    assert layer.query(q, principal=alice).cache_hit is True


# --------------------------------------------------------------------------- #
# Row-level governance
# --------------------------------------------------------------------------- #


def test_row_level_filter_restricts_data() -> None:
    store = StaticPolicyStore(
        by_role={
            "tenant_a": AccessPolicy(
                row_filter=Predicate(
                    field=FieldRef(name="book_id"), op=Comparison.EQ, value="book_a"
                )
            )
        }
    )
    layer = SemanticLayer(_graph(), make_engine(), governance=GovernanceEngine(store))
    p = Principal(subject="u", roles=frozenset({"tenant_a"}))
    out = layer.query(MetricQuery.of("total_video_seconds"), principal=p)
    # Only book_a's 4 shots * 5s = 20 (not the full 40).
    assert out.result.rows[0]["total_video_seconds"] == 20


def test_row_filter_conjoins_with_user_filter() -> None:
    store = StaticPolicyStore(
        by_role={
            "tenant_a": AccessPolicy(
                row_filter=Predicate(
                    field=FieldRef(name="book_id"), op=Comparison.EQ, value="book_a"
                )
            )
        }
    )
    layer = SemanticLayer(_graph(), make_engine(), governance=GovernanceEngine(store))
    p = Principal(subject="u", roles=frozenset({"tenant_a"}))
    # User additionally filters to showrunner; tenant filter ANDs on.
    out = layer.query(
        MetricQuery(
            metrics=("shot_total",),
            filters=(
                Predicate(field=FieldRef(name="agent_role"), op=Comparison.EQ, value="showrunner"),
            ),
        ),
        principal=p,
    )
    # book_a + showrunner = s1, s2 = 2 shots.
    assert out.result.rows[0]["shot_total"] == 2


# --------------------------------------------------------------------------- #
# Column-level governance
# --------------------------------------------------------------------------- #


def test_denied_metric_raises() -> None:
    store = StaticPolicyStore(
        by_role={"limited": AccessPolicy(allowed_metrics=frozenset({"shot_total"}))}
    )
    layer = SemanticLayer(_graph(), make_engine(), governance=GovernanceEngine(store))
    p = Principal(subject="u", roles=frozenset({"limited"}))
    layer.query(MetricQuery.of("shot_total"), principal=p)  # allowed
    with pytest.raises(AccessDenied):
        layer.query(MetricQuery.of("usd_total"), principal=p)


def test_denied_dimension_raises() -> None:
    store = StaticPolicyStore(
        by_role={"no_role": AccessPolicy(column_actions={"agent_role": ColumnAction.DENY})}
    )
    layer = SemanticLayer(_graph(), make_engine(), governance=GovernanceEngine(store))
    p = Principal(subject="u", roles=frozenset({"no_role"}))
    with pytest.raises(AccessDenied):
        layer.query(MetricQuery.of("shot_total", group_by=("agent_role",)), principal=p)


def test_masked_dimension_redacted_in_result() -> None:
    store = StaticPolicyStore(
        by_role={"mask_role": AccessPolicy(column_actions={"agent_role": ColumnAction.MASK})}
    )
    layer = SemanticLayer(_graph(), make_engine(), governance=GovernanceEngine(store))
    p = Principal(subject="u", roles=frozenset({"mask_role"}))
    out = layer.query(MetricQuery.of("shot_total", group_by=("agent_role",)), principal=p)
    assert out.masked_dimensions == ("agent_role",)
    assert all(r["agent_role"] == "***" for r in out.result.rows)
    # The metric values are unaffected by masking.
    assert sum(r["shot_total"] for r in out.result.rows) == 8


def test_governance_requires_principal() -> None:
    layer = SemanticLayer(
        _graph(), make_engine(), governance=GovernanceEngine(StaticPolicyStore())
    )
    with pytest.raises(ValueError):
        layer.query(MetricQuery.of("shot_total"))


def test_policy_merge_intersects_metrics_and_strengthens_columns() -> None:
    store = StaticPolicyStore(
        by_role={
            "r1": AccessPolicy(
                allowed_metrics=frozenset({"shot_total", "usd_total"}),
                column_actions={"agent_role": ColumnAction.MASK},
            ),
            "r2": AccessPolicy(
                allowed_metrics=frozenset({"shot_total", "regen_rate"}),
                column_actions={"agent_role": ColumnAction.DENY},
            ),
        }
    )
    merged = store.resolve(Principal(subject="u", roles=frozenset({"r1", "r2"})))
    assert merged.allowed_metrics == frozenset({"shot_total"})  # intersection
    assert merged.column_action("agent_role") == ColumnAction.DENY  # strongest


# --------------------------------------------------------------------------- #
# Advisor wiring
# --------------------------------------------------------------------------- #


def test_advisor_records_observed_queries() -> None:
    advisor = MaterializationAdvisor(min_frequency=2)
    layer = SemanticLayer(_graph(), make_engine(), advisor=advisor)
    q = MetricQuery.of("shot_total", group_by=("agent_role",))
    for _ in range(3):
        layer.query(q)
    assert advisor.observations == 3
    recs = layer.recommendations()
    assert recs  # the repeated shape crossed the threshold
