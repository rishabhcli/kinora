"""Freshness SLAs, lineage graph, and the monitoring aggregator."""

from __future__ import annotations

from datetime import timedelta

from app.lakehouse.features import (
    FeatureMonitor,
    FeatureRegistry,
    FeatureService,
    FreshnessState,
    OnDemandFeatureView,
    assess_freshness,
    build_lineage,
    freshness_report,
)
from app.lakehouse.features.freshness import FreshnessReport
from app.lakehouse.features.lineage import affected_services, column_view
from app.lakehouse.features.materialization import MaterializationResult
from app.lakehouse.features.parity import ParityReport, SkewReport
from app.lakehouse.features.types import FeatureSpec, ValueType

from .conftest import at, book_features_view, user_stats_view

# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #


def test_freshness_states() -> None:
    view = user_stats_view(ttl_minutes=60)
    now = at(100)
    # 10 min old → fresh.
    assert assess_freshness(view, event_timestamp=at(90), now=now).state == FreshnessState.FRESH
    # 70 min old (> 60 ttl) → stale.
    assert assess_freshness(view, event_timestamp=at(30), now=now).state == FreshnessState.STALE
    # missing.
    assert assess_freshness(view, event_timestamp=None, now=now).state == FreshnessState.MISSING


def test_freshness_warn_within_ttl_past_sla() -> None:
    view = user_stats_view(ttl_minutes=60)
    now = at(100)
    # 40 min old: within ttl(60) but past a 30-min SLA → WARN.
    a = assess_freshness(view, event_timestamp=at(60), now=now, sla=timedelta(minutes=30))
    assert a.state == FreshnessState.WARN


def test_freshness_report_sla_fraction() -> None:
    view = user_stats_view(ttl_minutes=60)
    now = at(100)
    report = freshness_report(
        view,
        event_timestamps=[at(95), at(90), at(10), None],  # fresh, fresh, stale, missing
        now=now,
    )
    assert report.fresh == 2 and report.stale == 1 and report.missing == 1
    assert report.sla_met_fraction == 0.5
    assert not report.ok


def test_freshness_future_value_is_fresh() -> None:
    view = user_stats_view(ttl_minutes=60)
    a = assess_freshness(view, event_timestamp=at(110), now=at(100))
    assert a.state == FreshnessState.FRESH
    assert a.age == timedelta(0)


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


def _lineage_registry() -> FeatureRegistry:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    reg.register_feature_view(book_features_view())
    reg.register_feature_service(
        FeatureService(name="svc_a", features=("user_stats:pages_read",))
    )
    odv = OnDemandFeatureView(
        name="derived",
        features=(FeatureSpec(name="x", dtype=ValueType.FLOAT),),
        source_views=("user_stats",),
    )
    reg.register_on_demand_view(odv, lambda req, up: {"x": 1.0})
    return reg


def test_lineage_graph_nodes_and_edges() -> None:
    graph = build_lineage(_lineage_registry())
    kinds = {n.kind for n in graph.nodes}
    assert kinds == {"source", "feature_view", "on_demand_view", "feature_service"}
    relations = {e.relation for e in graph.edges}
    assert relations == {"produces", "derives", "consumes"}


def test_lineage_upstream_of_service() -> None:
    reg = _lineage_registry()
    graph = build_lineage(reg)
    view = reg.get_feature_view("user_stats")
    svc_id = "service:svc_a"
    upstream = graph.upstream(svc_id)
    # The service's ancestors include its feature view and that view's source.
    assert f"view:user_stats@{view.version}" in upstream
    assert "source:user_stats_src" in upstream


def test_lineage_downstream_blast_radius() -> None:
    reg = _lineage_registry()
    assert "svc_a" in list(affected_services(reg, "user_stats"))
    # book_feats feeds no service → empty blast radius.
    assert list(affected_services(reg, "book_feats")) == []


def test_column_view_helper() -> None:
    assert column_view("user_stats__pages_read") == "user_stats"
    assert column_view("bare") is None


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #


def test_monitor_online_hit_rate() -> None:
    monitor = FeatureMonitor()
    monitor.record_online_read(hit=True)
    monitor.record_online_read(hit=True)
    monitor.record_online_read(hit=False)
    snap = monitor.snapshot()
    assert snap.counters["online_reads"] == 3
    assert snap.counters["online_hits"] == 2
    assert snap.online_hit_rate == 2 / 3


def test_monitor_records_view_health() -> None:
    monitor = FeatureMonitor()
    monitor.record_materialization(
        MaterializationResult(view="v", version=1, as_of=at(0), rows_written=8, keys_total=10)
    )
    monitor.record_parity(
        "v", ParityReport(per_feature=())
    )
    monitor.record_skew("v", SkewReport(per_feature=(), threshold=0.1))
    monitor.record_freshness(
        FreshnessReport(view="v", total=10, fresh=9, warn=0, stale=1, missing=0)
    )
    snap = monitor.snapshot()
    health = snap.view_health["v"]
    assert health.rows_materialized == 8
    assert health.last_materialized_coverage == 0.8
    assert health.parity_match_rate == 1.0
    assert health.skew_drifted == 0
    assert health.freshness_sla == 0.9


def test_monitor_reset() -> None:
    monitor = FeatureMonitor()
    monitor.incr("x", 5)
    monitor.reset()
    assert monitor.snapshot().counters == {}
