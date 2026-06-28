"""Product-analytics API — the batched, idempotent event-ingest + query surface.

* ``POST /api/analytics/events`` — **batched, idempotent** ingestion. The client
  posts a list of typed events; the server validates the closed taxonomy, scrubs
  each (PII-safe), and appends with dedupe on ``event_id`` (a retried batch is a
  no-op). Returns the accepted / newly-stored counts. Auth-gated; the caller's
  user id is used as the default ``user_ref`` when an event omits one (so the
  scrubber can pseudonymise it).
* ``POST /api/analytics/query`` — the flexible time-bucketed query (metric +
  granularity + filters + group-by). Returns dense series.
* ``GET  /api/analytics/engagement`` — population reading-engagement summary.
* ``POST /api/analytics/funnel`` — ordered-step funnel.
* ``GET  /api/analytics/retention`` — cohort-retention matrix.

This surface is **distinct** from ``/api/eval`` (the §13 crew-vs-baseline quality
report) and from the Prometheus ``/metrics`` ops surface — it answers "how do
humans use the product?".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.analytics.events import EventName, RawEvent
from app.analytics.query import Filters, MetricSpec, parse_metric
from app.analytics.query import Query as AnalyticsQuery
from app.analytics.timebucket import Granularity
from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger

logger = get_logger("app.api.analytics")

router = APIRouter(prefix="/analytics", tags=["analytics"])

#: Default query window when a caller omits ``since`` (days back from ``until``).
_DEFAULT_WINDOW_DAYS = 30
#: Hard cap on a query window (days) so a request can never scan unbounded time.
_MAX_WINDOW_DAYS = 730


def _require_enabled(container: ContainerDep) -> None:
    if not container.settings.analytics_enabled:
        raise APIError("analytics_disabled", "product analytics is disabled", status=404)


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


class IngestEvent(BaseModel):
    """One event in an ingest batch (the wire shape of :class:`RawEvent`)."""

    event_id: str = Field(min_length=1, max_length=128)
    name: str
    occurred_at: datetime
    book_id: str | None = None
    session_ref: str | None = None
    mode: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    """A batched ingest request."""

    events: list[IngestEvent] = Field(default_factory=list)


class IngestResponse(BaseModel):
    """The ingest outcome (idempotency surfaced via ``new`` < ``accepted``)."""

    received: int
    accepted: int
    new: int
    rejected: int
    errors: list[str] = Field(default_factory=list)


@router.post("/events", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    body: IngestRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> IngestResponse:
    """Idempotently ingest a batch of product-analytics events (PII-scrubbed)."""
    _require_enabled(container)
    if not body.events:
        return IngestResponse(received=0, accepted=0, new=0, rejected=0)

    raw: list[RawEvent] = []
    errors: list[str] = []
    for item in body.events:
        try:
            raw.append(
                RawEvent(
                    event_id=item.event_id,
                    name=item.name,
                    occurred_at=item.occurred_at,
                    # The authenticated user is the default subject; the scrubber
                    # pseudonymises it so the raw id never lands in storage.
                    user_ref=user.id,
                    book_id=item.book_id,
                    session_ref=item.session_ref,
                    mode=item.mode,  # str -> ReadMode coerced by pydantic / rejected
                    props=item.props,
                )
            )
        except ValueError as exc:
            errors.append(f"{item.event_id}: {exc}")

    if not raw:
        return IngestResponse(
            received=len(body.events),
            accepted=0,
            new=0,
            rejected=len(body.events),
            errors=errors[:20],
        )

    result = await container.analytics_service().ingest(raw)
    return IngestResponse(
        received=len(body.events),
        accepted=result.accepted,
        new=result.new,
        rejected=result.rejected + len(errors),
        errors=(result.errors + errors)[:20],
    )


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #


def _resolve_window(since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
    from datetime import UTC

    now = datetime.now(UTC)
    end = until or now
    start = since or (end - timedelta(days=_DEFAULT_WINDOW_DAYS))
    if end <= start:
        raise APIError("invalid_window", "`until` must be after `since`", status=422)
    if (end - start) > timedelta(days=_MAX_WINDOW_DAYS):
        raise APIError(
            "window_too_large",
            f"query window exceeds {_MAX_WINDOW_DAYS} days",
            status=422,
        )
    return start, end


def _parse_granularity(value: str | None) -> Granularity:
    if value is None:
        return Granularity.DAY
    try:
        return Granularity(value)
    except ValueError as exc:
        raise APIError("invalid_granularity", f"unknown granularity: {value}", status=422) from exc


def _parse_event_names(values: list[str] | None) -> tuple[EventName, ...] | None:
    if not values:
        return None
    names: list[EventName] = []
    for value in values:
        if not EventName.is_known(value):
            raise APIError("unknown_event", f"unknown event name: {value}", status=422)
        names.append(EventName(value))
    return tuple(names)


def _parse_metric(spec: str) -> MetricSpec:
    try:
        return parse_metric(spec)
    except ValueError as exc:
        raise APIError("invalid_metric", str(exc), status=422) from exc


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


class QueryRequest(BaseModel):
    """A flexible time-bucketed query request."""

    metric: str = "count"
    granularity: str = "day"
    since: datetime | None = None
    until: datetime | None = None
    names: list[str] | None = None
    book_id: str | None = None
    group_by: str | None = None
    prop_equals: dict[str, str] = Field(default_factory=dict)
    top_n: int | None = Field(default=None, ge=1, le=100)


class SeriesPointModel(BaseModel):
    bucket: str
    value: float


class SeriesModel(BaseModel):
    group: str
    total: float
    points: list[SeriesPointModel]


class QueryResponse(BaseModel):
    metric: str
    granularity: str
    buckets: list[str]
    series: list[SeriesModel]


@router.post("/query", response_model=QueryResponse)
async def run_analytics_query(
    body: QueryRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> QueryResponse:
    """Run a metric/granularity/group-by query over the event log."""
    _require_enabled(container)
    start, end = _resolve_window(body.since, body.until)
    metric = _parse_metric(body.metric)
    names = _parse_event_names(body.names)
    try:
        query = AnalyticsQuery(
            metric=metric,
            since=start,
            until=end,
            granularity=_parse_granularity(body.granularity),
            filters=Filters(names=names, book_id=body.book_id, prop_equals=body.prop_equals),
            group_by=body.group_by,
            top_n=body.top_n,
        )
        result = await container.analytics_service().run(query)
    except ValueError as exc:
        raise APIError("invalid_query", str(exc), status=422) from exc
    return QueryResponse(
        metric=result.metric,
        granularity=result.granularity.value,
        buckets=result.buckets,
        series=[
            SeriesModel(
                group=s.group,
                total=s.total,
                points=[SeriesPointModel(bucket=p.bucket, value=p.value) for p in s.points],
            )
            for s in result.series
        ],
    )


# --------------------------------------------------------------------------- #
# Engagement
# --------------------------------------------------------------------------- #


class EngagementResponse(BaseModel):
    session_count: int
    unique_readers: int
    unique_books: int
    total_reading_seconds: float
    median_session_seconds: float | None
    median_pages_per_min: float | None
    mean_pages_per_min: float | None
    median_words_per_min: float | None
    mean_completion_ratio: float | None
    completion_rate: float | None
    director_session_rate: float | None
    stall_rate: float | None
    dropoff_histogram: dict[int, int]
    completion_buckets: dict[str, int]


@router.get("/engagement", response_model=EngagementResponse)
async def get_engagement(
    container: ContainerDep,
    user: CurrentUser,
    book_id: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> EngagementResponse:
    """Population reading-engagement summary over the windowed sessions (§5.3)."""
    _require_enabled(container)
    start, end = _resolve_window(since, until)
    summary = await container.analytics_service().engagement(
        since=start, until=end, book_id=book_id
    )
    return EngagementResponse(
        session_count=summary.session_count,
        unique_readers=summary.unique_readers,
        unique_books=summary.unique_books,
        total_reading_seconds=summary.total_reading_seconds,
        median_session_seconds=summary.median_session_seconds,
        median_pages_per_min=summary.median_pages_per_min,
        mean_pages_per_min=summary.mean_pages_per_min,
        median_words_per_min=summary.median_words_per_min,
        mean_completion_ratio=summary.mean_completion_ratio,
        completion_rate=summary.completion_rate,
        director_session_rate=summary.director_session_rate,
        stall_rate=summary.stall_rate,
        dropoff_histogram=summary.dropoff_histogram,
        completion_buckets=summary.completion_buckets,
    )


# --------------------------------------------------------------------------- #
# Funnel
# --------------------------------------------------------------------------- #


class FunnelRequest(BaseModel):
    steps: list[str] = Field(min_length=1)
    since: datetime | None = None
    until: datetime | None = None
    window_hours: float | None = Field(default=None, gt=0)


class FunnelStepModel(BaseModel):
    name: str
    users: int
    conversion_from_start: float
    conversion_from_prev: float
    dropoff_from_prev: int


class FunnelResponse(BaseModel):
    steps: list[FunnelStepModel]
    total_entered: int
    total_converted: int
    overall_conversion: float
    median_time_to_convert_s: float | None


@router.post("/funnel", response_model=FunnelResponse)
async def run_funnel(
    body: FunnelRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> FunnelResponse:
    """Ordered-step funnel over the windowed population."""
    _require_enabled(container)
    start, end = _resolve_window(body.since, body.until)
    steps: list[EventName] = []
    for value in body.steps:
        if not EventName.is_known(value):
            raise APIError("unknown_event", f"unknown event name: {value}", status=422)
        steps.append(EventName(value))
    window = timedelta(hours=body.window_hours) if body.window_hours else None
    result = await container.analytics_service().funnel(
        steps, since=start, until=end, window=window
    )
    return FunnelResponse(
        steps=[
            FunnelStepModel(
                name=s.name.value,
                users=s.users,
                conversion_from_start=s.conversion_from_start,
                conversion_from_prev=s.conversion_from_prev,
                dropoff_from_prev=s.dropoff_from_prev,
            )
            for s in result.steps
        ],
        total_entered=result.total_entered,
        total_converted=result.total_converted,
        overall_conversion=result.overall_conversion,
        median_time_to_convert_s=result.median_time_to_convert_s,
    )


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


class CohortRowModel(BaseModel):
    cohort_label: str
    size: int
    retained: dict[int, int]


class RetentionResponse(BaseModel):
    granularity: str
    max_offset: int
    rolling: bool
    cohorts: list[CohortRowModel]
    average_curve: dict[int, float]


@router.get("/retention", response_model=RetentionResponse)
async def get_retention(
    container: ContainerDep,
    user: CurrentUser,
    granularity: Annotated[str, Query()] = "day",
    max_offset: Annotated[int, Query(ge=0, le=52)] = 7,
    rolling: Annotated[bool, Query()] = False,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> RetentionResponse:
    """Cohort-retention matrix (day or week) over the windowed population."""
    _require_enabled(container)
    start, end = _resolve_window(since, until)
    gran = _parse_granularity(granularity)
    if gran not in (Granularity.DAY, Granularity.WEEK):
        raise APIError(
            "invalid_granularity", "retention supports day or week only", status=422
        )
    matrix = await container.analytics_service().retention(
        granularity=gran,
        max_offset=max_offset,
        rolling=rolling,
        since=start,
        until=end,
    )
    return RetentionResponse(
        granularity=matrix.granularity.value,
        max_offset=matrix.max_offset,
        rolling=matrix.rolling,
        cohorts=[
            CohortRowModel(cohort_label=c.cohort_label, size=c.size, retained=c.retained)
            for c in matrix.cohorts
        ],
        average_curve=matrix.average_curve(),
    )


__all__ = ["router"]
