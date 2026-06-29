"""Factory — assemble the Kinora §13 KPI semantic layer.

A convenience builder that wires the §13 KPI metric definitions
(:mod:`app.lakehouse.semantic.kpis`) onto the render-telemetry star schema and
returns either a bare :class:`SemanticGraph` (for compiling/lineage/catalog) or a
fully-composed :class:`SemanticLayer` (governance + cache + advisor) ready for a
self-serve query route.

The physical model here mirrors the §12.5 telemetry the render pipeline already
emits (per-shot: seconds, accepted, regens, ccs, spend; per-buffer-sample:
above-L, stalled). It is *declarative only* — no DB calls — so a deployment
points the engine (facet A or the SQL fallback) at whatever physical tables hold
that telemetry. The model/measure names are the contract; the sources
(``fact_shots``, ``fact_buffer``, ``dim_books``) are the warehouse table names.
"""

from __future__ import annotations

from app.lakehouse.semantic.advisor import MaterializationAdvisor
from app.lakehouse.semantic.cache import InMemoryResultCache
from app.lakehouse.semantic.engine import QueryEngine
from app.lakehouse.semantic.governance import GovernanceEngine, PolicyResolver
from app.lakehouse.semantic.kpis import (
    KPI_CATALOG_TAGS,
    buffer_kpi_metrics,
    kpi_metrics,
)
from app.lakehouse.semantic.model import (
    Dimension,
    Join,
    JoinType,
    Measure,
    SemanticModel,
)
from app.lakehouse.semantic.registry import SemanticGraph
from app.lakehouse.semantic.service import SemanticLayer
from app.lakehouse.semantic.types import (
    Aggregation,
    Comparison,
    DataType,
    FieldRef,
    Predicate,
    TimeGrain,
)


def kinora_shots_model() -> SemanticModel:
    """The per-shot render-telemetry fact (the §13 KPI substrate)."""
    return SemanticModel(
        name="shots",
        source="fact_shots",
        primary_entity="shot_id",
        label="Render Shots",
        description="One row per rendered shot (the §12.5 per-shot telemetry).",
        dimensions=(
            Dimension(name="shot_id", data_type=DataType.STRING),
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(name="agent_role", data_type=DataType.STRING, label="Agent Role"),
            Dimension(name="mode", data_type=DataType.STRING, description="Render mode (§9.3)."),
            Dimension(
                name="rendered_at",
                data_type=DataType.TIMESTAMP,
                is_time=True,
                base_grain=TimeGrain.HOUR,
                label="Rendered At",
            ),
        ),
        measures=(
            Measure(name="shot_count", agg=Aggregation.COUNT, expr=None),
            Measure(name="total_seconds", agg=Aggregation.SUM, expr="seconds"),
            Measure(
                name="rejected_seconds",
                agg=Aggregation.SUM,
                expr="seconds",
                measure_filter=Predicate(
                    field=FieldRef(name="accepted"), op=Comparison.EQ, value=False
                ),
            ),
            Measure(name="regen_count", agg=Aggregation.SUM, expr="regens"),
            Measure(name="ccs_sum", agg=Aggregation.SUM, expr="ccs"),
            Measure(name="usd_spent", agg=Aggregation.SUM, expr="usd"),
        ),
        joins=(
            Join(
                to_model="books",
                from_key="book_id",
                to_key="book_id",
                join_type=JoinType.LEFT,
                many_to_one=True,
            ),
        ),
    )


def kinora_books_model() -> SemanticModel:
    return SemanticModel(
        name="books",
        source="dim_books",
        primary_entity="book_id",
        label="Books",
        dimensions=(
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(name="title", data_type=DataType.STRING),
            Dimension(name="genre", data_type=DataType.STRING),
        ),
        measures=(Measure(name="book_count", agg=Aggregation.COUNT_DISTINCT, expr="book_id"),),
    )


def kinora_buffer_model() -> SemanticModel:
    return SemanticModel(
        name="buffer",
        source="fact_buffer",
        primary_entity="sample_id",
        label="Buffer Samples",
        description="One row per committed-buffer occupancy sample (§5.3/§13).",
        dimensions=(
            Dimension(name="sample_id", data_type=DataType.STRING),
            Dimension(name="book_id", data_type=DataType.STRING),
            Dimension(
                name="sampled_at",
                data_type=DataType.TIMESTAMP,
                is_time=True,
                base_grain=TimeGrain.HOUR,
            ),
        ),
        measures=(
            Measure(name="sample_count", agg=Aggregation.COUNT, expr=None),
            Measure(name="above_low_count", agg=Aggregation.SUM_BOOLEAN, expr="above_low"),
            Measure(name="stall_count", agg=Aggregation.SUM_BOOLEAN, expr="stalled"),
        ),
    )


def build_kinora_graph() -> SemanticGraph:
    """The validated semantic graph for the §13 KPIs."""
    return SemanticGraph.build(
        [kinora_shots_model(), kinora_books_model(), kinora_buffer_model()],
        [*kpi_metrics(), *buffer_kpi_metrics()],
    )


def build_kinora_layer(
    engine: QueryEngine,
    *,
    policy_resolver: PolicyResolver | None = None,
    cache_ttl_seconds: float = 300.0,
    cache_max_entries: int = 512,
    enable_advisor: bool = True,
) -> SemanticLayer:
    """Compose a ready-to-serve §13 KPI :class:`SemanticLayer`.

    ``engine`` is facet A (or the SQL-backed engine, or
    :class:`InMemoryEngine`). Governance is enabled only when a
    ``policy_resolver`` is supplied; otherwise every ask is allowed (single-tenant
    default). The result cache + materialization advisor are on by default.
    """
    governance = GovernanceEngine(policy_resolver) if policy_resolver is not None else None
    cache = InMemoryResultCache(
        max_entries=cache_max_entries, ttl_seconds=cache_ttl_seconds
    )
    advisor = MaterializationAdvisor() if enable_advisor else None
    return SemanticLayer(
        build_kinora_graph(),
        engine,
        governance=governance,
        cache=cache,
        advisor=advisor,
        catalog_tags=dict(KPI_CATALOG_TAGS),
    )


__all__ = [
    "build_kinora_graph",
    "build_kinora_layer",
    "kinora_books_model",
    "kinora_buffer_model",
    "kinora_shots_model",
]
