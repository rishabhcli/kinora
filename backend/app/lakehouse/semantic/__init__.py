"""Facet C — the governed metrics-as-code semantic layer.

A declarative semantic model (entities/dimensions/measures/joins) + a
metric-definition language (simple/ratio/derived/cumulative/time-comparison,
filtered) compiled deterministically to engine query plans (with a SQL
fallback), behind a self-serve query API with row/column governance, result
caching, a materialization advisor, metric lineage, and a metrics catalog. The
§13 Kinora KPIs (accepted-footage efficiency, regen rate, CCS, budget burn,
buffer health) ship as the reference metric set.

The public surface is small and composed by :class:`SemanticLayer` /
:func:`build_kinora_layer`; the sub-modules stay importable for advanced use.
"""

from __future__ import annotations

from app.lakehouse.semantic.advisor import (
    AggregationShape,
    MaterializationAdvisor,
    Recommendation,
)
from app.lakehouse.semantic.cache import (
    CacheStats,
    InMemoryResultCache,
    ResultCache,
    cache_key,
)
from app.lakehouse.semantic.catalog import (
    DimensionEntry,
    MetricEntry,
    MetricsCatalog,
)
from app.lakehouse.semantic.compiler import CompileError, Compiler, compile_query
from app.lakehouse.semantic.engine import (
    AggregateResult,
    InMemoryEngine,
    QueryEngine,
)
from app.lakehouse.semantic.executor import MetricResult, execute_plan
from app.lakehouse.semantic.factory import (
    build_kinora_graph,
    build_kinora_layer,
)
from app.lakehouse.semantic.governance import (
    AccessDenied,
    AccessPolicy,
    ColumnAction,
    GovernanceEngine,
    Principal,
    StaticPolicyStore,
)
from app.lakehouse.semantic.kpis import buffer_kpi_metrics, kpi_metrics
from app.lakehouse.semantic.lineage import LineageGraph, MetricLineage
from app.lakehouse.semantic.loader import LoaderError, load_filter, load_graph
from app.lakehouse.semantic.metrics import (
    CalculationKind,
    CumulativeMetric,
    DerivedMetric,
    Metric,
    RatioMetric,
    SimpleMetric,
    TimeComparisonMetric,
    WindowKind,
)
from app.lakehouse.semantic.model import (
    Dimension,
    Join,
    Measure,
    SemanticModel,
)
from app.lakehouse.semantic.plan import QueryPlan
from app.lakehouse.semantic.query import MetricQuery, TimeWindow
from app.lakehouse.semantic.registry import (
    MeasureRef,
    SemanticGraph,
    SemanticGraphError,
)
from app.lakehouse.semantic.serialize import (
    catalog_to_dict,
    plan_to_dict,
    result_to_dict,
    result_to_series,
)
from app.lakehouse.semantic.service import QueryOutcome, SemanticLayer
from app.lakehouse.semantic.sql import RenderedSql, SqlRenderer, render_sql
from app.lakehouse.semantic.types import (
    Aggregation,
    Comparison,
    DataType,
    FieldRef,
    OrderBy,
    Predicate,
    SortDirection,
    TimeGrain,
)

__all__ = [
    # types
    "Aggregation",
    "Comparison",
    "DataType",
    "FieldRef",
    "OrderBy",
    "Predicate",
    "SortDirection",
    "TimeGrain",
    # model
    "Dimension",
    "Join",
    "Measure",
    "SemanticModel",
    # metrics
    "CalculationKind",
    "CumulativeMetric",
    "DerivedMetric",
    "Metric",
    "RatioMetric",
    "SimpleMetric",
    "TimeComparisonMetric",
    "WindowKind",
    # registry
    "MeasureRef",
    "SemanticGraph",
    "SemanticGraphError",
    # query + plan
    "MetricQuery",
    "QueryPlan",
    "TimeWindow",
    # compile + execute
    "CompileError",
    "Compiler",
    "compile_query",
    "MetricResult",
    "execute_plan",
    # engine
    "AggregateResult",
    "InMemoryEngine",
    "QueryEngine",
    # sql fallback
    "RenderedSql",
    "SqlRenderer",
    "render_sql",
    # governance
    "AccessDenied",
    "AccessPolicy",
    "ColumnAction",
    "GovernanceEngine",
    "Principal",
    "StaticPolicyStore",
    # cache
    "CacheStats",
    "InMemoryResultCache",
    "ResultCache",
    "cache_key",
    # advisor
    "AggregationShape",
    "MaterializationAdvisor",
    "Recommendation",
    # lineage + catalog
    "DimensionEntry",
    "LineageGraph",
    "MetricEntry",
    "MetricLineage",
    "MetricsCatalog",
    # loader (declarative metrics-as-code)
    "LoaderError",
    "load_filter",
    "load_graph",
    # serialisation (JSON-safe DTOs)
    "catalog_to_dict",
    "plan_to_dict",
    "result_to_dict",
    "result_to_series",
    # service + factory + kpis
    "QueryOutcome",
    "SemanticLayer",
    "build_kinora_graph",
    "build_kinora_layer",
    "buffer_kpi_metrics",
    "kpi_metrics",
]
