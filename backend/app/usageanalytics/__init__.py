"""Cost & usage analytics + dashboards (see ``DESIGN.md`` in this package).

A **time-series analytics warehouse over spend**: it rolls cost/usage/quality
events up by provider / model / book / session / time-bucket, detects anomalies
(spend spike, error surge, quality regression), projects budget burndown +
month-end spend vs a ``$30``-style cap, attributes cost ($/finished-minute of
film), and serves it all read-only to a dashboard UI.

Distinct from its three neighbours (all kept untouched):

* :mod:`app.optim.cost_meter` — a single process-global "since boot" rollup.
* :mod:`app.finops` — budget *governance* (tiered caps, video-seconds forecast,
  the promote/optimize/halt decision).
* :mod:`app.analytics` — the *product* event pipeline (reading behaviour).

Every pure module (``events``, ``window``, ``aggregate``, ``anomaly``,
``burndown``, ``attribution``) imports no infrastructure and is safe to import
anywhere. The persistence seam is
:class:`app.usageanalytics.store.UsageMetricStore`; the in-memory implementation
is the default for tests and a viable embedded backend. The
:class:`app.usageanalytics.service.UsageAnalyticsService` is the façade the API
route reads through.
"""

from __future__ import annotations

from app.usageanalytics.aggregate import (
    Metric,
    grouped,
    leaderboard,
    series,
    totals,
)
from app.usageanalytics.anomaly import (
    Alert,
    AnomalyKind,
    DetectorConfig,
    Severity,
    detect_all,
)
from app.usageanalytics.attribution import (
    CostBreakdown,
    CostShare,
    UnitEconomics,
    cost_breakdown,
    unit_economics,
)
from app.usageanalytics.burndown import (
    BurndownPoint,
    BurndownReport,
    DailyCost,
    build_burndown,
)
from app.usageanalytics.events import MetricCell, Provider, UsageEvent, infer_provider
from app.usageanalytics.service import ServiceConfig, UsageAnalyticsService
from app.usageanalytics.store import (
    BOOK,
    MODEL,
    PROVIDER,
    SESSION,
    Dimension,
    InMemoryUsageMetricStore,
    RedisUsageMetricStore,
    UsageMetricStore,
)
from app.usageanalytics.window import (
    Granularity,
    RetentionPolicy,
    RetentionTier,
    Window,
    sliding_windows,
    tumbling_windows,
)

__all__ = [
    "BOOK",
    "MODEL",
    "PROVIDER",
    "SESSION",
    "Alert",
    "AnomalyKind",
    "BurndownPoint",
    "BurndownReport",
    "CostBreakdown",
    "CostShare",
    "DailyCost",
    "DetectorConfig",
    "Dimension",
    "Granularity",
    "InMemoryUsageMetricStore",
    "Metric",
    "MetricCell",
    "Provider",
    "RedisUsageMetricStore",
    "RetentionPolicy",
    "RetentionTier",
    "ServiceConfig",
    "Severity",
    "UnitEconomics",
    "UsageAnalyticsService",
    "UsageEvent",
    "UsageMetricStore",
    "Window",
    "build_burndown",
    "cost_breakdown",
    "detect_all",
    "grouped",
    "infer_provider",
    "leaderboard",
    "series",
    "sliding_windows",
    "totals",
    "tumbling_windows",
    "unit_economics",
]
