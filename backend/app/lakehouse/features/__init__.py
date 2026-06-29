"""Kinora feature store — lakehouse facet B (online + offline feature platform).

A feature platform in the Feast lineage, built natively into the Kinora backend
and tuned to its conventions (string ids, the §8 shared embedding space, the
"recall the relevant slice, never the whole log" discipline). It serves the
existing recommendations engine (``app.recommendations``) and any future ML, and
is **distinct** from it: recommendations is candidate-generation→scoring→re-rank;
this is the typed *feature platform* — definitions, point-in-time-correct training
joins, low-latency online serving, parity/skew/freshness/lineage — that such
models consume.

Public surface (see ``DESIGN.md`` for the module map):

* **types** — the contract value objects (:class:`Entity`, :class:`FeatureView`,
  :class:`FeatureService`, :class:`FeatureRef`, :class:`ValueType`, …).
* **rows** — :class:`FeatureRow` / :class:`EntityRow` / :class:`Frame`.
* **registry** — :class:`FeatureRegistry` (content-addressed versioning).
* **pit** — :func:`point_in_time_join` (the anti-leakage training join).
* **offline_store** / **online_store** — historical + serving stores.
* **materialization** — offline→online :func:`materialize`.
* **parity** — :func:`check_parity` + :func:`detect_skew`.
* **freshness** / **lineage** / **monitoring** — operational quality.
* **on_demand** — request-time + streaming computation seam.
* **store** — the :class:`FeatureStore` façade.
"""

from __future__ import annotations

from .freshness import FreshnessReport, FreshnessState, assess_freshness, freshness_report
from .lineage import LineageGraph, build_lineage
from .materialization import MaterializationResult, materialize
from .monitoring import FeatureMonitor, MonitorSnapshot
from .offline_store import (
    EngineOfflineStore,
    InMemoryOfflineStore,
    get_historical_features,
)
from .on_demand import apply_on_demand, days_since, push_stream_rows
from .online_store import (
    InMemoryOnlineStore,
    OnlineValue,
    RedisOnlineStore,
    get_online_features,
)
from .parity import (
    ParityReport,
    SkewReport,
    check_parity,
    detect_skew,
    population_stability_index,
)
from .pit import JoinedFeature, point_in_time_join, point_in_time_lookup
from .registry import FeatureRegistry
from .rows import EntityRow, FeatureRow, Frame
from .store import FeatureStore
from .types import (
    DefinitionError,
    Entity,
    FeatureRef,
    FeatureService,
    FeatureSource,
    FeatureSpec,
    FeatureStoreError,
    FeatureView,
    OnDemandFeatureView,
    PointInTimeError,
    ReferenceError,
    Transformation,
    ValueType,
)

__all__ = [
    "DefinitionError",
    "EngineOfflineStore",
    "Entity",
    "EntityRow",
    "FeatureMonitor",
    "FeatureRef",
    "FeatureRegistry",
    "FeatureRow",
    "FeatureService",
    "FeatureSource",
    "FeatureSpec",
    "FeatureStore",
    "FeatureStoreError",
    "FeatureView",
    "Frame",
    "FreshnessReport",
    "FreshnessState",
    "InMemoryOfflineStore",
    "InMemoryOnlineStore",
    "JoinedFeature",
    "LineageGraph",
    "MaterializationResult",
    "MonitorSnapshot",
    "OnDemandFeatureView",
    "OnlineValue",
    "ParityReport",
    "PointInTimeError",
    "RedisOnlineStore",
    "ReferenceError",
    "SkewReport",
    "Transformation",
    "ValueType",
    "apply_on_demand",
    "assess_freshness",
    "build_lineage",
    "check_parity",
    "days_since",
    "detect_skew",
    "freshness_report",
    "get_historical_features",
    "get_online_features",
    "materialize",
    "point_in_time_join",
    "point_in_time_lookup",
    "population_stability_index",
    "push_stream_rows",
]
