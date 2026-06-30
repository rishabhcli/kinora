"""The roll-up engine: bucketed cells → dense series, grouped tables, leaderboards.

Pure functions over a :class:`~app.usageanalytics.store.UsageMetricStore`. The
store buckets + filters; this module shapes the results a dashboard wants:

* :func:`series` — a **dense** time-series of one metric (no gaps; missing
  buckets are zero/None), at a chosen granularity, optionally downsampled.
* :func:`grouped` — totals broken down by one or more axes
  (``provider``/``model``/``book``/``session``) over a window.
* :func:`leaderboard` — the top-N rows of a :func:`grouped` table sorted by a
  metric (e.g. costliest models, slowest by p95).
* :func:`totals` — a single aggregate :class:`MetricCell` over the window.

A *metric* is one of the named derived quantities a cell exposes; :func:`metric_of`
extracts it as a JSON-friendly value (Decimal cost rendered as a string).
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.usageanalytics.events import MetricCell
from app.usageanalytics.store import Dimension, UsageMetricStore, project
from app.usageanalytics.window import Granularity, downsample_buckets


class Metric(enum.StrEnum):
    """A named, dashboard-selectable derived quantity of a :class:`MetricCell`."""

    COST_USD = "cost_usd"
    VIDEO_SECONDS = "video_seconds"
    AUDIO_SECONDS = "audio_seconds"
    IMAGES = "images"
    CALLS = "calls"
    ERRORS = "errors"
    TOKENS = "tokens"
    SUCCESS_RATE = "success_rate"
    ERROR_RATE = "error_rate"
    CACHE_HIT_RATE = "cache_hit_rate"
    LATENCY_P50 = "latency_p50_ms"
    LATENCY_P95 = "latency_p95_ms"
    QUALITY = "avg_quality"


def metric_of(cell: MetricCell, metric: Metric) -> Any:
    """Extract a metric from a cell as a JSON-friendly scalar.

    Cost is returned as a :class:`Decimal` (the caller stringifies it for JSON);
    rates are floats; latency percentiles / quality may be ``None``.
    """
    if metric is Metric.COST_USD:
        return cell.cost_usd
    if metric is Metric.VIDEO_SECONDS:
        return round(cell.video_seconds, 3)
    if metric is Metric.AUDIO_SECONDS:
        return round(cell.audio_seconds, 3)
    if metric is Metric.IMAGES:
        return cell.images
    if metric is Metric.CALLS:
        return cell.calls
    if metric is Metric.ERRORS:
        return cell.errors
    if metric is Metric.TOKENS:
        return cell.total_tokens
    if metric is Metric.SUCCESS_RATE:
        return round(cell.success_rate, 6)
    if metric is Metric.ERROR_RATE:
        return round(cell.error_rate, 6)
    if metric is Metric.CACHE_HIT_RATE:
        return round(cell.cache_hit_rate, 6)
    if metric is Metric.LATENCY_P50:
        v = cell.latency_percentile(0.5)
        return None if v is None else round(v, 3)
    if metric is Metric.LATENCY_P95:
        v = cell.latency_percentile(0.95)
        return None if v is None else round(v, 3)
    if metric is Metric.QUALITY:
        return cell.avg_quality
    return None  # pragma: no cover - exhaustive above


def _sort_value(cell: MetricCell, metric: Metric) -> float:
    """A comparable float for sorting (Decimal → float, None → -inf)."""
    v = metric_of(cell, metric)
    if v is None:
        return float("-inf")
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def totals(
    store: UsageMetricStore,
    *,
    since: datetime,
    until: datetime,
    where: Dimension | None = None,
    granularity: Granularity = Granularity.DAY,
) -> MetricCell:
    """A single aggregate cell over the whole window (all matching dimensions)."""
    where = where or Dimension()
    bucketed = store.cells(granularity, since, until, where)
    agg = MetricCell()
    for pairs in bucketed.values():
        for _dim, cell in pairs:
            agg.merge(cell)
    return agg


def series(
    store: UsageMetricStore,
    metric: Metric,
    *,
    since: datetime,
    until: datetime,
    granularity: Granularity = Granularity.DAY,
    where: Dimension | None = None,
    downsample_to: Granularity | None = None,
) -> list[dict[str, Any]]:
    """A **dense** time-series of ``metric``: one point per bucket in the range.

    Missing buckets are emitted with the metric's zero value (or ``None`` for
    latency/quality), so a chart never has to interpolate gaps.
    """
    where = where or Dimension()
    grain = (
        downsample_to
        if downsample_to is not None and downsample_to.is_coarser_than(granularity)
        else granularity
    )
    bucketed = store.cells(granularity, since, until, where)
    merged: dict[datetime, MetricCell] = {}
    for bucket, pairs in bucketed.items():
        agg = MetricCell()
        for _dim, cell in pairs:
            agg.merge(cell)
        merged[bucket] = agg
    if grain is not granularity:
        merged = downsample_buckets(merged, grain)

    out: list[dict[str, Any]] = []
    for bucket in grain.buckets(since, until):
        cell = merged.get(bucket, MetricCell())
        value = metric_of(cell, metric)
        if isinstance(value, Decimal):
            value = str(value)
        out.append({"bucket": bucket.isoformat(), "value": value})
    return out


def grouped(
    store: UsageMetricStore,
    *,
    axes: list[str],
    since: datetime,
    until: datetime,
    where: Dimension | None = None,
    granularity: Granularity = Granularity.DAY,
) -> dict[tuple[str, ...], MetricCell]:
    """Total metrics over the window, broken down by the requested ``axes``.

    Returns ``{group_key: aggregated_cell}`` where ``group_key`` is the projection
    of each concrete dimension onto ``axes``.
    """
    where = where or Dimension()
    bucketed = store.cells(granularity, since, until, where)
    out: dict[tuple[str, ...], MetricCell] = {}
    for pairs in bucketed.values():
        for dim, cell in pairs:
            key = project(dim, axes)
            agg = out.get(key)
            if agg is None:
                agg = MetricCell()
                out[key] = agg
            agg.merge(cell)
    return out


def leaderboard(
    store: UsageMetricStore,
    *,
    axes: list[str],
    metric: Metric,
    since: datetime,
    until: datetime,
    where: Dimension | None = None,
    granularity: Granularity = Granularity.DAY,
    limit: int = 10,
    descending: bool = True,
) -> list[dict[str, Any]]:
    """Top-N groups by ``metric`` over the window (the dashboard leaderboards).

    Each row carries the group key, the sort metric's value, and the full cell
    snapshot so the UI can show context (calls, cost, p95, …) in one shot.
    """
    table = grouped(
        store, axes=axes, since=since, until=until, where=where, granularity=granularity
    )
    rows = sorted(
        table.items(),
        key=lambda kv: _sort_value(kv[1], metric),
        reverse=descending,
    )
    out: list[dict[str, Any]] = []
    for key, cell in rows[: max(0, limit)]:
        value = metric_of(cell, metric)
        if isinstance(value, Decimal):
            value = str(value)
        out.append(
            {
                "key": dict(zip(axes, key, strict=False)),
                "metric": metric.value,
                "value": value,
                "cell": cell.as_dict(),
            }
        )
    return out


__all__ = [
    "Metric",
    "grouped",
    "leaderboard",
    "metric_of",
    "series",
    "totals",
]
