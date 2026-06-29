"""Metric-lineage + catalog tests."""

from __future__ import annotations

from app.lakehouse.semantic.catalog import MetricsCatalog
from app.lakehouse.semantic.kpis import (
    KPI_CATALOG_TAGS,
    buffer_kpi_metrics,
    kpi_metrics,
)
from app.lakehouse.semantic.lineage import LineageGraph, NodeKind
from app.lakehouse.semantic.metrics import MetricKind
from app.lakehouse.semantic.registry import SemanticGraph
from tests.lakehouse_fixtures import books_model, buffer_model, shots_model


def _graph() -> SemanticGraph:
    return SemanticGraph.build(
        [shots_model(), books_model(), buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


def test_lineage_upstream_of_derived_kpi() -> None:
    lg = LineageGraph(_graph())
    up = lg.upstream("accepted_footage_efficiency")
    # The derived efficiency depends on rejected + total simple metrics.
    assert "rejected_video_seconds" in up
    assert "total_video_seconds" in up


def test_lineage_base_measures_and_columns() -> None:
    lg = LineageGraph(_graph())
    lineage = lg.lineage_of("accepted_footage_efficiency")
    # bottoms out in the two seconds measures (rejected uses a filtered column key).
    measure_keys = set(lineage.base_measures)
    assert any(k.startswith("shots.total_seconds") for k in measure_keys)
    assert any(k.startswith("shots.rejected_seconds") for k in measure_keys)
    assert lineage.models == ("shots",)
    assert all(c.source == "fact_shots" for c in lineage.physical_columns)


def test_lineage_downstream_impact() -> None:
    lg = LineageGraph(_graph())
    # total_video_seconds feeds accepted_footage_efficiency (and is itself simple).
    down = lg.downstream("total_video_seconds")
    assert "accepted_footage_efficiency" in down


def test_lineage_edges_typed() -> None:
    lg = LineageGraph(_graph())
    edges = lg.to_edges(["ccs"])
    kinds = {(e.source_kind, e.target_kind) for e in edges}
    # ccs is a ratio over ccs_total / shot_total -> metric->metric edges plus
    # measure/column/model edges for its simple inputs.
    assert (NodeKind.METRIC, NodeKind.METRIC) in kinds
    assert (NodeKind.MEASURE, NodeKind.METRIC) in kinds
    assert (NodeKind.COLUMN, NodeKind.MEASURE) in kinds
    assert (NodeKind.MODEL, NodeKind.COLUMN) in kinds


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


def test_catalog_lists_all_metrics() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    names = {m.name for m in cat.metrics()}
    assert "accepted_footage_efficiency" in names
    assert "buffer_health" in names


def test_catalog_describe_carries_metadata() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    entry = cat.describe("accepted_footage_efficiency")
    assert entry.kind == MetricKind.DERIVED
    assert entry.format == "percent"
    assert "headline" in entry.tags
    assert "rejected_video_seconds" in entry.upstream_metrics


def test_catalog_search_ranks_name_over_description() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    results = cat.search("ccs")
    assert results
    assert results[0].name == "ccs"  # exact name beats substring/desc hits


def test_catalog_search_by_tag_and_groups() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    budget = {m.name for m in cat.by_tag("budget")}
    assert "budget_burn" in budget
    groups = cat.groups()
    assert "headline" in groups
    assert "accepted_footage_efficiency" in groups["headline"]


def test_catalog_time_dependent_flag() -> None:
    cat = MetricsCatalog(_graph(), tags=KPI_CATALOG_TAGS)
    assert cat.describe("budget_burn").time_dependent is True
    assert cat.describe("regen_rate").time_dependent is False


def test_catalog_dimensions_listed() -> None:
    cat = MetricsCatalog(_graph())
    dims = {(d.model, d.name) for d in cat.dimensions()}
    assert ("shots", "agent_role") in dims
    assert any(d.is_time for d in cat.dimensions())


def test_catalog_kinds_index() -> None:
    cat = MetricsCatalog(_graph())
    kinds = cat.kinds()
    assert "ratio" in kinds
    assert "derived" in kinds
    assert "cumulative" in kinds
    assert "time_comparison" in kinds
