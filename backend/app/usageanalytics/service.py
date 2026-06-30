"""The dashboard service: one façade the API route reads through.

Ties the store + aggregation engine + anomaly detectors + burndown + attribution
into the handful of read-shaped operations the dashboard API exposes, plus the
single ingest path. It owns the configured retention policy, detector config, and
monthly USD cap so the route stays a thin adapter.

Construction is infra-free: pass an :class:`~app.usageanalytics.store.UsageMetricStore`
(the in-memory one by default). :meth:`from_settings` reads the additive
``ua_*`` settings. The service never spends, never blocks, and never raises on
ordinary input.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.usageanalytics.aggregate import (
    Metric,
    grouped,
    leaderboard,
    series,
    totals,
)
from app.usageanalytics.anomaly import Alert, DetectorConfig, detect_all
from app.usageanalytics.attribution import cost_breakdown, unit_economics
from app.usageanalytics.burndown import (
    BurndownReport,
    build_burndown,
    daily_from_isoseries,
)
from app.usageanalytics.events import MetricCell, UsageEvent
from app.usageanalytics.store import (
    Dimension,
    InMemoryUsageMetricStore,
    UsageMetricStore,
)
from app.usageanalytics.window import Granularity, RetentionPolicy

logger = get_logger("app.usageanalytics.service")


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Tunables the service reads (overridable from :class:`Settings`)."""

    retention: RetentionPolicy
    detector: DetectorConfig
    monthly_cap_usd: Decimal
    default_window_days: int = 30
    max_window_days: int = 730
    run_rate_window_days: int = 7

    @classmethod
    def default(cls) -> ServiceConfig:
        return cls(
            retention=RetentionPolicy.default(),
            detector=DetectorConfig(),
            monthly_cap_usd=Decimal("30"),
        )


class UsageAnalyticsService:
    """The read-only dashboard façade (+ the one ingest path)."""

    def __init__(
        self,
        store: UsageMetricStore | None = None,
        config: ServiceConfig | None = None,
    ) -> None:
        self.store: UsageMetricStore = store or InMemoryUsageMetricStore()
        self.config = config or ServiceConfig.default()

    @classmethod
    def from_settings(cls, settings: Any) -> UsageAnalyticsService:
        """Build from the additive ``ua_*`` settings, falling back to defaults."""
        cfg = ServiceConfig.default()
        cap = getattr(settings, "ua_monthly_cap_usd", None)
        d = cfg.detector
        detector = DetectorConfig(
            spend_spike_ratio=float(getattr(settings, "ua_spend_spike_ratio", d.spend_spike_ratio)),
            error_surge_delta=float(getattr(settings, "ua_error_surge_delta", d.error_surge_delta)),
            quality_drop_delta=float(
                getattr(settings, "ua_quality_drop_delta", d.quality_drop_delta)
            ),
        )
        config = ServiceConfig(
            retention=cfg.retention,
            detector=detector,
            monthly_cap_usd=Decimal(str(cap)) if cap is not None else cfg.monthly_cap_usd,
            default_window_days=int(
                getattr(settings, "ua_default_window_days", cfg.default_window_days)
            ),
            max_window_days=int(getattr(settings, "ua_max_window_days", cfg.max_window_days)),
            run_rate_window_days=int(
                getattr(settings, "ua_run_rate_window_days", cfg.run_rate_window_days)
            ),
        )
        return cls(config=config)

    # --- ingest ----------------------------------------------------------- #

    def record(self, ev: UsageEvent) -> None:
        self.store.record(ev)

    def ingest(self, events: Iterable[UsageEvent]) -> int:
        n = self.store.record_many(events)
        logger.debug("usageanalytics.ingest", events=n)
        return n

    def prune(self, now: datetime) -> int:
        return self.store.prune(now, self.config.retention)

    # --- reads ------------------------------------------------------------ #

    def series(
        self,
        metric: Metric,
        *,
        since: datetime,
        until: datetime,
        granularity: Granularity = Granularity.DAY,
        where: Dimension | None = None,
        downsample_to: Granularity | None = None,
    ) -> list[dict[str, Any]]:
        return series(
            self.store,
            metric,
            since=since,
            until=until,
            granularity=granularity,
            where=where,
            downsample_to=downsample_to,
        )

    def totals(
        self,
        *,
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
        granularity: Granularity = Granularity.DAY,
    ) -> dict[str, Any]:
        return totals(
            self.store, since=since, until=until, where=where, granularity=granularity
        ).as_dict()

    def grouped(
        self,
        *,
        axes: list[str],
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
        granularity: Granularity = Granularity.DAY,
    ) -> list[dict[str, Any]]:
        table = grouped(
            self.store,
            axes=axes,
            since=since,
            until=until,
            where=where,
            granularity=granularity,
        )
        return [
            {"key": dict(zip(axes, key, strict=False)), "cell": cell.as_dict()}
            for key, cell in sorted(
                table.items(), key=lambda kv: float(kv[1].cost_usd), reverse=True
            )
        ]

    def leaderboard(
        self,
        *,
        axes: list[str],
        metric: Metric,
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
        limit: int = 10,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        return leaderboard(
            self.store,
            axes=axes,
            metric=metric,
            since=since,
            until=until,
            where=where,
            limit=limit,
            descending=descending,
        )

    def anomalies(
        self,
        *,
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
        granularity: Granularity = Granularity.HOUR,
    ) -> list[Alert]:
        buckets = _rollup_via_cells(
            self.store, granularity, since, until, where or Dimension()
        )
        return detect_all(buckets, self.config.detector)

    def burndown(
        self,
        *,
        as_of: datetime,
        where: Dimension | None = None,
        cap_usd: Decimal | None = None,
    ) -> BurndownReport:
        # Pull the month's daily cost series and project it.
        month_start = as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        until = as_of + timedelta(days=1)
        cost_series = self.series(
            Metric.COST_USD,
            since=month_start,
            until=until,
            granularity=Granularity.DAY,
            where=where,
        )
        daily = daily_from_isoseries(cost_series)
        return build_burndown(
            daily,
            cap_usd=cap_usd if cap_usd is not None else self.config.monthly_cap_usd,
            as_of=as_of.date(),
            run_rate_window_days=self.config.run_rate_window_days,
        )

    def cost_breakdown(
        self,
        *,
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
    ) -> dict[str, Any]:
        return cost_breakdown(
            self.store, since=since, until=until, where=where
        ).as_dict()

    def unit_economics(
        self,
        *,
        since: datetime,
        until: datetime,
        where: Dimension | None = None,
    ) -> dict[str, Any]:
        return unit_economics(self.store, since=since, until=until, where=where).as_dict()

    def event_count(self) -> int:
        return self.store.event_count()


def _rollup_via_cells(
    store: UsageMetricStore,
    granularity: Granularity,
    since: datetime,
    until: datetime,
    where: Dimension,
) -> dict[datetime, MetricCell]:
    """Total all dimensions per bucket (the series the anomaly detectors read)."""
    raw = store.cells(granularity, since, until, where)
    merged: dict[datetime, MetricCell] = {}
    for bucket, pairs in raw.items():
        agg = MetricCell()
        for _dim, cell in pairs:
            agg.merge(cell)
        merged[bucket] = agg
    return merged


__all__ = [
    "ServiceConfig",
    "UsageAnalyticsService",
]
