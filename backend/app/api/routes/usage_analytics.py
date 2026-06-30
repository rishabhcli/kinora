"""Cost & usage analytics dashboard API — read-only roll-ups/series/forecasts.

The operator/FinOps dashboard surface over :mod:`app.usageanalytics` (kinora.md
§11.1). Every endpoint is **read-only** and behind auth (spend is sensitive); the
whole surface 404s when ``usage_analytics_enabled`` is off. It never spends and
never enables live video.

* ``GET /usage-analytics/overview`` — the single-call dashboard summary: window
  totals + the latest anomalies + the burndown headline.
* ``GET /usage-analytics/series`` — a dense time-series of one metric
  (cost/video-seconds/calls/error-rate/p95/quality…), at a granularity, filtered
  by provider/model/book/session.
* ``GET /usage-analytics/totals`` — the aggregate cell over the window.
* ``GET /usage-analytics/leaderboard`` — top-N groups (provider/model/book/session)
  by a metric (costliest models, slowest by p95, …).
* ``GET /usage-analytics/anomalies`` — fired spend-spike / error-surge /
  quality-regression alerts over the window.
* ``GET /usage-analytics/burndown`` — month-to-date burndown + projected month-end
  spend vs the cap + ETA-to-cap.
* ``GET /usage-analytics/attribution`` — cost split by provider/model/book (with
  shares).
* ``GET /usage-analytics/unit-economics`` — $/finished-minute-of-film, overall
  and per book.

Distinct from ``/api/analytics`` (product behaviour) and ``/api/finops`` (budget
governance over video-seconds).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.core.logging import get_logger
from app.usageanalytics.aggregate import Metric
from app.usageanalytics.store import BOOK, MODEL, PROVIDER, SESSION, Dimension
from app.usageanalytics.window import Granularity

logger = get_logger("app.api.usage_analytics")

router = APIRouter(prefix="/usage-analytics", tags=["usage-analytics"])

#: The group-by axes a leaderboard / breakdown accepts.
_AXES = {"provider": PROVIDER, "model": MODEL, "book": BOOK, "session": SESSION}


# --------------------------------------------------------------------------- #
# Guards + shared parsing
# --------------------------------------------------------------------------- #


def _service(container: ContainerDep) -> Any:
    if not getattr(container.settings, "usage_analytics_enabled", True):
        raise APIError(
            "usage_analytics_disabled", "cost & usage analytics is disabled", status=404
        )
    return container.usage_analytics_service()


def _window(
    container: ContainerDep,
    since: datetime | None,
    until: datetime | None,
) -> tuple[datetime, datetime]:
    svc_cfg = container.usage_analytics_service().config
    end = until or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start = since or (end - timedelta(days=svc_cfg.default_window_days))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end <= start:
        raise APIError("invalid_window", "until must be after since", status=422)
    if (end - start).days > svc_cfg.max_window_days:
        raise APIError(
            "window_too_large",
            f"query window exceeds {svc_cfg.max_window_days} days",
            status=422,
        )
    return start, end


def _granularity(value: str | None) -> Granularity:
    if value is None:
        return Granularity.DAY
    try:
        return Granularity(value)
    except ValueError as exc:
        raise APIError("invalid_granularity", f"unknown granularity: {value}", status=422) from exc


def _metric(value: str | None, default: Metric = Metric.COST_USD) -> Metric:
    if value is None:
        return default
    try:
        return Metric(value)
    except ValueError as exc:
        raise APIError("invalid_metric", f"unknown metric: {value}", status=422) from exc


def _dimension(
    provider: str | None,
    model: str | None,
    book_id: str | None,
    session_id: str | None,
) -> Dimension:
    prov = None
    if provider is not None:
        from app.usageanalytics.events import Provider

        try:
            prov = Provider(provider)
        except ValueError as exc:
            raise APIError(
                "invalid_provider", f"unknown provider: {provider}", status=422
            ) from exc
    return Dimension(provider=prov, model=model, book_id=book_id, session_id=session_id)


def _axes(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    out: list[str] = []
    for raw in value.split(","):
        axis = raw.strip()
        if axis not in _AXES:
            raise APIError("invalid_axis", f"unknown axis: {axis}", status=422)
        out.append(_AXES[axis])
    return out


# --------------------------------------------------------------------------- #
# Response models (thin — the service returns JSON-ready dicts)
# --------------------------------------------------------------------------- #


class SeriesResponse(BaseModel):
    metric: str
    granularity: str
    since: str
    until: str
    points: list[dict[str, Any]]


class OverviewResponse(BaseModel):
    since: str
    until: str
    totals: dict[str, Any]
    anomalies: list[dict[str, Any]]
    burndown: dict[str, Any]
    top_models: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/series", response_model=SeriesResponse)
async def get_series(
    container: ContainerDep,
    _user: CurrentUser,
    metric: Annotated[str | None, Query()] = None,
    granularity: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    downsample_to: Annotated[str | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
) -> SeriesResponse:
    svc = _service(container)
    start, end = _window(container, since, until)
    m = _metric(metric)
    gran = _granularity(granularity)
    ds = _granularity(downsample_to) if downsample_to else None
    where = _dimension(provider, model, book_id, session_id)
    points = svc.series(
        m, since=start, until=end, granularity=gran, where=where, downsample_to=ds
    )
    return SeriesResponse(
        metric=m.value,
        granularity=(ds or gran).value,
        since=start.isoformat(),
        until=end.isoformat(),
        points=points,
    )


@router.get("/totals")
async def get_totals(
    container: ContainerDep,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    start, end = _window(container, since, until)
    where = _dimension(provider, model, book_id, session_id)
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "totals": svc.totals(since=start, until=end, where=where),
    }


@router.get("/leaderboard")
async def get_leaderboard(
    container: ContainerDep,
    _user: CurrentUser,
    by: Annotated[str | None, Query(description="comma-separated axes")] = None,
    metric: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    ascending: Annotated[bool, Query()] = False,
    provider: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    start, end = _window(container, since, until)
    axes = _axes(by, default=[MODEL])
    m = _metric(metric)
    where = _dimension(provider, None, book_id, None)
    rows = svc.leaderboard(
        axes=axes,
        metric=m,
        since=start,
        until=end,
        where=where,
        limit=limit,
        descending=not ascending,
    )
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "by": axes,
        "metric": m.value,
        "rows": rows,
    }


@router.get("/anomalies")
async def get_anomalies(
    container: ContainerDep,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    granularity: Annotated[str | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    start, end = _window(container, since, until)
    gran = _granularity(granularity) if granularity else Granularity.HOUR
    where = _dimension(provider, model, book_id, None)
    alerts = svc.anomalies(since=start, until=end, where=where, granularity=gran)
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "granularity": gran.value,
        "alerts": [a.as_dict() for a in alerts],
    }


@router.get("/burndown")
async def get_burndown(
    container: ContainerDep,
    _user: CurrentUser,
    as_of: Annotated[datetime | None, Query()] = None,
    cap_usd: Annotated[float | None, Query(ge=0.0)] = None,
    provider: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    when = as_of or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    where = _dimension(provider, None, book_id, None)
    cap = Decimal(str(cap_usd)) if cap_usd is not None else None
    report = svc.burndown(as_of=when, where=where, cap_usd=cap)
    return report.as_dict()


@router.get("/attribution")
async def get_attribution(
    container: ContainerDep,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    start, end = _window(container, since, until)
    where = _dimension(provider, None, book_id, None)
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "breakdown": svc.cost_breakdown(since=start, until=end, where=where),
    }


@router.get("/unit-economics")
async def get_unit_economics(
    container: ContainerDep,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    svc = _service(container)
    start, end = _window(container, since, until)
    where = _dimension(provider, None, book_id, None)
    return {
        "since": start.isoformat(),
        "until": end.isoformat(),
        "unit_economics": svc.unit_economics(since=start, until=end, where=where),
    }


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    container: ContainerDep,
    _user: CurrentUser,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
) -> OverviewResponse:
    """The single-call dashboard summary (totals + anomalies + burndown + leaders)."""
    svc = _service(container)
    start, end = _window(container, since, until)
    where = _dimension(provider, None, book_id, None)
    alerts = svc.anomalies(since=start, until=end, where=where, granularity=Granularity.HOUR)
    burndown = svc.burndown(as_of=end, where=where)
    top = svc.leaderboard(
        axes=[MODEL], metric=Metric.COST_USD, since=start, until=end, where=where, limit=5
    )
    return OverviewResponse(
        since=start.isoformat(),
        until=end.isoformat(),
        totals=svc.totals(since=start, until=end, where=where),
        anomalies=[a.as_dict() for a in alerts],
        burndown=burndown.as_dict(),
        top_models=top,
    )
