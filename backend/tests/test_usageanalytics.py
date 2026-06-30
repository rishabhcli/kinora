"""Cost & usage analytics + dashboards tests (app/usageanalytics/, kinora.md §11.1).

Fully deterministic and infra-free: an in-memory metric store + a fixed clock,
and the dashboard router behind a fake container with auth bypassed (mirrors
``test_api_finops.py``). Never enables KINORA_LIVE_VIDEO / spends.

Coverage:
* aggregation correctness across windows + group-by + leaderboards;
* time-bucketing, downsampling, retention pruning;
* anomaly triggers (spend spike, error surge, quality regression) + non-triggers;
* burndown + month-end forecast + ETA-to-cap math;
* cost attribution + $/finished-minute unit economics;
* the dashboard API responses via TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container, get_current_user
from app.api.errors import install_exception_handlers
from app.api.routes.usage_analytics import router
from app.core.config import Settings
from app.usageanalytics import (
    BOOK,
    MODEL,
    DailyCost,
    DetectorConfig,
    Dimension,
    Granularity,
    InMemoryUsageMetricStore,
    Metric,
    Provider,
    RetentionPolicy,
    ServiceConfig,
    UsageAnalyticsService,
    UsageEvent,
    build_burndown,
    cost_breakdown,
    detect_all,
    grouped,
    leaderboard,
    series,
    totals,
    unit_economics,
)
from app.usageanalytics.anomaly import (
    AnomalyKind,
    Severity,
    detect_error_surge,
    detect_quality_regression,
    detect_spend_spike,
)
from app.usageanalytics.events import MetricCell, infer_provider, percentile
from app.usageanalytics.window import (
    RetentionTier,
    downsample_buckets,
    sliding_windows,
    tumbling_windows,
)

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


def _ev(
    *,
    at: datetime,
    model: str = "qwen3.7-max",
    operation: str = "chat",
    cost: str = "0",
    video_s: float = 0.0,
    latency_ms: float | None = None,
    success: bool = True,
    cache_hit: bool = False,
    quality: float | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
    provider: Provider | None = None,
) -> UsageEvent:
    return UsageEvent(
        at=at,
        model=model,
        operation=operation,
        provider=provider or Provider.UNKNOWN,
        cost_usd=Decimal(cost),
        video_seconds=video_s,
        latency_ms=latency_ms,
        success=success,
        cache_hit=cache_hit,
        quality=quality,
        book_id=book_id,
        session_id=session_id,
    )


# --------------------------------------------------------------------------- #
# events / cells
# --------------------------------------------------------------------------- #


def test_provider_inference() -> None:
    assert infer_provider("wan2.1-i2v-turbo") is Provider.WAN
    assert infer_provider("qwen3.7-max") is Provider.DASHSCOPE
    assert infer_provider("MiniMax-Video-01") is Provider.MINIMAX
    assert infer_provider("gpt-5.5") is Provider.OPENAI
    assert infer_provider("mystery-model") is Provider.UNKNOWN


def test_event_normalises_provider_and_tz() -> None:
    ev = UsageEvent(at=datetime(2026, 1, 1), model="wan2.7-i2v", operation="video")
    assert ev.provider is Provider.WAN
    assert ev.at.tzinfo is UTC


def test_percentile_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    xs = [10.0, 20.0, 30.0, 40.0]
    assert percentile(xs, 0.5) == 20.0  # ceil(0.5*4)=2 -> 20
    assert percentile(xs, 0.95) == 40.0
    assert percentile(xs, 1.0) == 40.0
    assert percentile(xs, 0.0) == 10.0


def test_metric_cell_derived_metrics() -> None:
    cell = MetricCell()
    for ok in (True, True, True, False):  # 1 error / 4 calls
        cell.add(_ev(at=T0, success=ok, latency_ms=100.0, cache_hit=ok, quality=0.8))
    assert cell.calls == 4
    assert cell.errors == 1
    assert cell.success_rate == 0.75
    assert cell.error_rate == 0.25
    assert cell.cache_hit_rate == 0.75
    assert cell.avg_quality == 0.8
    assert cell.latency_percentile(0.5) == 100.0


def test_metric_cell_empty_defaults() -> None:
    cell = MetricCell()
    assert cell.success_rate == 1.0
    assert cell.error_rate == 0.0
    assert cell.avg_quality is None
    assert cell.latency_percentile(0.95) is None


# --------------------------------------------------------------------------- #
# windows / downsampling / retention
# --------------------------------------------------------------------------- #


def test_tumbling_windows_cover_range_without_gaps() -> None:
    wins = tumbling_windows(T0, T0 + timedelta(hours=3), timedelta(hours=1))
    assert len(wins) == 3
    assert wins[0].start == T0
    assert wins[-1].end == T0 + timedelta(hours=3)
    # back-to-back, no gaps
    for a, b in zip(wins, wins[1:], strict=False):
        assert a.end == b.start


def test_tumbling_last_window_clamped() -> None:
    wins = tumbling_windows(T0, T0 + timedelta(minutes=150), timedelta(hours=1))
    assert len(wins) == 3
    assert wins[-1].duration_s == 30 * 60


def test_sliding_windows_overlap() -> None:
    wins = sliding_windows(T0, T0 + timedelta(hours=2), timedelta(hours=1), timedelta(minutes=30))
    assert len(wins) == 4
    assert wins[0].start == T0
    assert wins[1].start == T0 + timedelta(minutes=30)


def test_degenerate_windows_empty() -> None:
    assert tumbling_windows(T0, T0, timedelta(hours=1)) == []
    assert sliding_windows(T0, T0 + timedelta(hours=1), timedelta(hours=1), timedelta(0)) == []


def test_granularity_floor_and_buckets() -> None:
    at = datetime(2026, 6, 15, 13, 47, 30, tzinfo=UTC)
    assert Granularity.DAY.floor(at) == datetime(2026, 6, 15, tzinfo=UTC)
    assert Granularity.HOUR.floor(at) == datetime(2026, 6, 15, 13, tzinfo=UTC)
    assert Granularity.MONTH.floor(at) == datetime(2026, 6, 1, tzinfo=UTC)
    days = list(Granularity.DAY.buckets(T0, T0 + timedelta(days=3)))
    assert days == [T0, T0 + timedelta(days=1), T0 + timedelta(days=2)]


def test_month_bucket_rollover() -> None:
    dec = datetime(2026, 12, 5, tzinfo=UTC)
    assert Granularity.MONTH.next_bucket(Granularity.MONTH.floor(dec)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_downsample_hour_to_day_merges_cells() -> None:
    buckets = {
        T0: _cell(cost="1", calls=1),
        T0 + timedelta(hours=1): _cell(cost="2", calls=1),
        T0 + timedelta(days=1): _cell(cost="4", calls=1),
    }
    daily = downsample_buckets(buckets, Granularity.DAY)
    assert set(daily) == {T0, T0 + timedelta(days=1)}
    assert daily[T0].cost_usd == Decimal("3")
    assert daily[T0].calls == 2
    assert daily[T0 + timedelta(days=1)].cost_usd == Decimal("4")


def _cell(*, cost: str, calls: int) -> MetricCell:
    c = MetricCell()
    for _ in range(calls):
        c.add(_ev(at=T0, cost=cost if calls == 1 else "0"))
    if calls != 1:
        c.cost_usd = Decimal(cost)
    return c


def test_retention_policy_tiers_and_prune() -> None:
    policy = RetentionPolicy(
        tiers=(
            RetentionTier(Granularity.MINUTE, timedelta(hours=6)),
            RetentionTier(Granularity.DAY, timedelta(days=30)),
        )
    )
    assert policy.horizon == timedelta(days=30)
    minute_tier = policy.tier_for_age(timedelta(hours=1))
    day_tier = policy.tier_for_age(timedelta(days=10))
    assert minute_tier is not None and minute_tier.granularity is Granularity.MINUTE
    assert day_tier is not None and day_tier.granularity is Granularity.DAY
    assert policy.tier_for_age(timedelta(days=99)) is None

    store = InMemoryUsageMetricStore()
    store.record(_ev(at=T0))  # old
    store.record(_ev(at=T0 + timedelta(days=40)))  # newer
    now = T0 + timedelta(days=40)
    dropped = store.prune(now, policy)
    assert dropped == 1
    assert store.event_count() == 1


# --------------------------------------------------------------------------- #
# aggregation engine
# --------------------------------------------------------------------------- #


def _seed_store() -> InMemoryUsageMetricStore:
    store = InMemoryUsageMetricStore()
    # Day 0: two models, two books, one cached.
    store.record(
        _ev(at=T0, model="wan2.7-i2v", operation="video", cost="1.20", video_s=10.0,
            latency_ms=2000.0, book_id="b1", quality=0.9)
    )
    store.record(
        _ev(at=T0 + timedelta(hours=1), model="qwen3.7-max", cost="0.05",
            latency_ms=100.0, book_id="b1", cache_hit=True, quality=0.8)
    )
    # Day 1: more spend on b2.
    store.record(
        _ev(at=T0 + timedelta(days=1), model="wan2.7-i2v", operation="video", cost="2.40",
            video_s=20.0, latency_ms=3000.0, book_id="b2", success=False)
    )
    return store


def test_totals_over_window() -> None:
    agg = totals(_seed_store(), since=T0, until=T0 + timedelta(days=2))
    assert agg.calls == 3
    assert agg.errors == 1
    assert agg.cost_usd == Decimal("3.65")
    assert agg.video_seconds == 30.0


def test_series_dense_no_gaps() -> None:
    store = _seed_store()
    pts = series(store, Metric.COST_USD, since=T0, until=T0 + timedelta(days=3),
                 granularity=Granularity.DAY)
    assert len(pts) == 3  # 3 dense day buckets
    assert pts[0]["value"] == "1.25"  # day 0: 1.20 + 0.05
    assert pts[1]["value"] == "2.40"
    assert pts[2]["value"] == "0"  # gap filled with zero


def test_series_downsample_hour_to_day() -> None:
    store = _seed_store()
    hourly = series(store, Metric.CALLS, since=T0, until=T0 + timedelta(days=1),
                    granularity=Granularity.HOUR, downsample_to=Granularity.DAY)
    assert len(hourly) == 1
    assert hourly[0]["value"] == 2  # both day-0 calls folded into one day bucket


def test_window_filters_out_of_range() -> None:
    store = _seed_store()
    agg = totals(store, since=T0, until=T0 + timedelta(days=1))  # only day 0
    assert agg.calls == 2
    assert agg.cost_usd == Decimal("1.25")


def test_grouped_by_model_and_book() -> None:
    store = _seed_store()
    by_model = grouped(store, axes=[MODEL], since=T0, until=T0 + timedelta(days=2))
    costs = {k[0]: v.cost_usd for k, v in by_model.items()}
    assert costs == {"wan2.7-i2v": Decimal("3.60"), "qwen3.7-max": Decimal("0.05")}

    by_book = grouped(store, axes=[BOOK], since=T0, until=T0 + timedelta(days=2))
    book_costs = {k[0]: v.cost_usd for k, v in by_book.items()}
    assert book_costs == {"b1": Decimal("1.25"), "b2": Decimal("2.40")}


def test_leaderboard_costliest_model_first() -> None:
    rows = leaderboard(_seed_store(), axes=[MODEL], metric=Metric.COST_USD,
                       since=T0, until=T0 + timedelta(days=2), limit=10)
    assert rows[0]["key"] == {"model": "wan2.7-i2v"}
    assert rows[0]["value"] == "3.60"
    assert rows[1]["key"] == {"model": "qwen3.7-max"}


def test_dimension_filter() -> None:
    store = _seed_store()
    agg = totals(store, since=T0, until=T0 + timedelta(days=2),
                 where=Dimension(book_id="b2"))
    assert agg.cost_usd == Decimal("2.40")
    assert agg.errors == 1


# --------------------------------------------------------------------------- #
# anomaly detection
# --------------------------------------------------------------------------- #


def _series_buckets(values: list[float], *, key: str = "cost") -> dict[datetime, MetricCell]:
    out: dict[datetime, MetricCell] = {}
    for i, v in enumerate(values):
        c = MetricCell()
        if key == "cost":
            c.cost_usd = Decimal(str(v))
            c.calls = 1
        out[T0 + timedelta(hours=i)] = c
    return out


def test_spend_spike_triggers() -> None:
    buckets = _series_buckets([1.0, 1.0, 1.0, 10.0])
    alert = detect_spend_spike(buckets)
    assert alert is not None
    assert alert.kind is AnomalyKind.SPEND_SPIKE
    assert alert.severity >= Severity.WARNING
    assert alert.observed == 10.0


def test_spend_spike_no_trigger_on_steady() -> None:
    buckets = _series_buckets([1.0, 1.1, 0.9, 1.05])
    assert detect_spend_spike(buckets) is None


def test_spend_spike_respects_noise_floor() -> None:
    # Big ratio but tiny absolute cost -> below the $0.50 floor, no alert.
    buckets = _series_buckets([0.001, 0.001, 0.001, 0.10])
    assert detect_spend_spike(buckets) is None


def test_error_surge_triggers_with_volume() -> None:
    buckets: dict[datetime, MetricCell] = {}
    for i in range(4):
        c = MetricCell()
        for _ in range(50):
            c.add(_ev(at=T0, success=True))
        buckets[T0 + timedelta(hours=i)] = c
    # Latest bucket: half fail.
    last = MetricCell()
    for j in range(50):
        last.add(_ev(at=T0, success=j % 2 == 0))
    buckets[T0 + timedelta(hours=4)] = last
    alert = detect_error_surge(buckets)
    assert alert is not None
    assert alert.kind is AnomalyKind.ERROR_SURGE
    assert alert.severity is Severity.CRITICAL  # >= 0.5 absolute


def test_error_surge_ignores_low_volume() -> None:
    buckets: dict[datetime, MetricCell] = {}
    for i in range(4):
        c = MetricCell()
        c.add(_ev(at=T0, success=True))
        buckets[T0 + timedelta(hours=i)] = c
    last = MetricCell()
    last.add(_ev(at=T0, success=False))  # 1/1 fail, but min_calls=20
    buckets[T0 + timedelta(hours=4)] = last
    assert detect_error_surge(buckets) is None


def test_quality_regression_triggers() -> None:
    buckets: dict[datetime, MetricCell] = {}
    for i, q in enumerate([0.9, 0.9, 0.9]):
        c = MetricCell()
        for _ in range(20):
            c.add(_ev(at=T0, quality=q))
        buckets[T0 + timedelta(hours=i)] = c
    last = MetricCell()
    for _ in range(20):
        last.add(_ev(at=T0, quality=0.6))  # big drop
    buckets[T0 + timedelta(hours=3)] = last
    alert = detect_quality_regression(buckets)
    assert alert is not None
    assert alert.kind is AnomalyKind.QUALITY_REGRESSION
    assert alert.severity is Severity.CRITICAL


def test_quality_regression_no_trigger_when_stable() -> None:
    buckets: dict[datetime, MetricCell] = {}
    for i in range(4):
        c = MetricCell()
        for _ in range(20):
            c.add(_ev(at=T0, quality=0.85))
        buckets[T0 + timedelta(hours=i)] = c
    assert detect_quality_regression(buckets) is None


def test_detect_all_sorts_by_severity() -> None:
    buckets = _series_buckets([1.0, 1.0, 1.0, 100.0])
    alerts = detect_all(buckets)
    assert len(alerts) >= 1
    assert all(
        alerts[i].severity >= alerts[i + 1].severity for i in range(len(alerts) - 1)
    )


def test_custom_detector_config() -> None:
    cfg = DetectorConfig(spend_spike_ratio=1.5, spend_spike_min_usd=0.01)
    buckets = _series_buckets([1.0, 1.0, 1.0, 2.0])
    assert detect_spend_spike(buckets, cfg) is not None  # 2x > 1.5x ratio


# --------------------------------------------------------------------------- #
# burndown + forecast
# --------------------------------------------------------------------------- #


def test_burndown_projects_month_end_over_cap() -> None:
    # $2/day for the first 10 days of a 30-day month, cap $30.
    daily = [DailyCost(day=(T0 + timedelta(days=i)).date(), cost_usd=Decimal("2"))
             for i in range(10)]
    as_of = (T0 + timedelta(days=9)).date()  # June 10
    report = build_burndown(daily, cap_usd=Decimal("30"), as_of=as_of)
    assert report.spent_mtd_usd == Decimal("20.000000")
    assert report.run_rate_usd_per_day == Decimal("2.000000")
    assert report.days_remaining == 20
    # 20 spent + 2*20 projected = 60 > 30 cap.
    assert report.projected_month_end_usd == Decimal("60.000000")
    assert report.will_exceed is True
    assert report.projected_overage_usd == Decimal("30.000000")
    assert report.eta_to_cap is not None


def test_burndown_under_cap_no_eta() -> None:
    daily = [DailyCost(day=(T0 + timedelta(days=i)).date(), cost_usd=Decimal("0.10"))
             for i in range(10)]
    as_of = (T0 + timedelta(days=9)).date()
    report = build_burndown(daily, cap_usd=Decimal("30"), as_of=as_of)
    assert report.will_exceed is False
    assert report.eta_to_cap is None
    # Curve spans the whole month.
    assert len(report.curve) == 30
    assert report.curve[-1].remaining_usd > Decimal("0")


def test_burndown_eta_date_math() -> None:
    # $5/day, $30 cap, $10 spent by June 2 -> 4 more days to hit cap (June 6).
    daily = [
        DailyCost(day=datetime(2026, 6, 1, tzinfo=UTC).date(), cost_usd=Decimal("5")),
        DailyCost(day=datetime(2026, 6, 2, tzinfo=UTC).date(), cost_usd=Decimal("5")),
    ]
    report = build_burndown(daily, cap_usd=Decimal("30"),
                            as_of=datetime(2026, 6, 2, tzinfo=UTC).date(),
                            run_rate_window_days=2)
    # run-rate = (5+5)/2 = 5/day; remaining budget 20 / 5 = 4 days -> June 6.
    assert report.run_rate_usd_per_day == Decimal("5.000000")
    assert report.eta_to_cap == datetime(2026, 6, 6, tzinfo=UTC).date()


def test_burndown_already_over_cap() -> None:
    daily = [DailyCost(day=datetime(2026, 6, 1, tzinfo=UTC).date(), cost_usd=Decimal("50"))]
    report = build_burndown(daily, cap_usd=Decimal("30"),
                            as_of=datetime(2026, 6, 1, tzinfo=UTC).date())
    assert report.spent_mtd_usd == Decimal("50.000000")
    assert report.eta_to_cap == datetime(2026, 6, 1, tzinfo=UTC).date()  # "now"


# --------------------------------------------------------------------------- #
# attribution + unit economics
# --------------------------------------------------------------------------- #


def test_cost_breakdown_shares_sum_to_one() -> None:
    bd = cost_breakdown(_seed_store(), since=T0, until=T0 + timedelta(days=2))
    assert bd.total_usd == Decimal("3.65")
    assert bd.by_model[0].key == "wan2.7-i2v"  # costliest first
    share_sum = sum(s.share for s in bd.by_model)
    assert abs(share_sum - 1.0) < 1e-9


def test_unit_economics_dollars_per_finished_minute() -> None:
    ue = unit_economics(_seed_store(), since=T0, until=T0 + timedelta(days=2))
    # 30 video-seconds = 0.5 finished minutes; total $3.65 -> $7.30/min.
    assert abs(ue.finished_minutes - 0.5) < 1e-9
    assert ue.cost_per_finished_minute_usd == Decimal("7.300000")
    # b1 made 10s of film for $1.25 -> $7.50/min; b2 20s for $2.40 -> $7.20/min.
    by_book = {r["book_id"]: r for r in ue.per_book}
    assert by_book["b1"]["cost_per_finished_minute_usd"] == "7.500000"
    assert by_book["b2"]["cost_per_finished_minute_usd"] == "7.200000"


def test_unit_economics_no_video_is_none() -> None:
    store = InMemoryUsageMetricStore()
    store.record(_ev(at=T0, model="qwen3.7-max", cost="1.00", video_s=0.0, book_id="b1"))
    ue = unit_economics(store, since=T0, until=T0 + timedelta(days=1))
    assert ue.cost_per_finished_minute_usd is None
    assert ue.per_book[0]["cost_per_finished_minute_usd"] is None


# --------------------------------------------------------------------------- #
# service façade
# --------------------------------------------------------------------------- #


def test_service_from_settings_reads_cap() -> None:
    settings = Settings(dashscope_api_key="test", ua_monthly_cap_usd=12.5,
                        ua_spend_spike_ratio=4.0)
    svc = UsageAnalyticsService.from_settings(settings)
    assert svc.config.monthly_cap_usd == Decimal("12.5")
    assert svc.config.detector.spend_spike_ratio == 4.0


def test_service_burndown_uses_store() -> None:
    store = InMemoryUsageMetricStore()
    for i in range(5):
        store.record(_ev(at=T0 + timedelta(days=i), cost="3.00"))
    svc = UsageAnalyticsService(
        store=store,
        config=ServiceConfig(
            retention=RetentionPolicy.default(),
            detector=DetectorConfig(),
            monthly_cap_usd=Decimal("30"),
            run_rate_window_days=5,
        ),
    )
    report = svc.burndown(as_of=T0 + timedelta(days=4))
    assert report.spent_mtd_usd == Decimal("15.000000")
    assert report.run_rate_usd_per_day == Decimal("3.000000")


# --------------------------------------------------------------------------- #
# dashboard API (TestClient, fake container, auth bypassed)
# --------------------------------------------------------------------------- #


class _FakeContainer:
    def __init__(self, settings: Settings, svc: UsageAnalyticsService) -> None:
        self.settings = settings
        self._svc = svc

    def usage_analytics_service(self) -> UsageAnalyticsService:
        return self._svc


def _client(svc: UsageAnalyticsService | None = None, *, enabled: bool = True) -> TestClient:
    settings = Settings(dashscope_api_key="test", usage_analytics_enabled=enabled)
    svc = svc or _seeded_service()
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router, prefix="/api")
    fake = _FakeContainer(settings, svc)
    app.dependency_overrides[get_container] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


def _seeded_service() -> UsageAnalyticsService:
    svc = UsageAnalyticsService(store=_seed_store())
    return svc


def test_api_series() -> None:
    resp = _client().get(
        "/api/usage-analytics/series",
        params={"metric": "cost_usd", "granularity": "day",
                "since": T0.isoformat(), "until": (T0 + timedelta(days=3)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["metric"] == "cost_usd"
    assert len(data["points"]) == 3
    assert data["points"][0]["value"] == "1.25"


def test_api_totals() -> None:
    resp = _client().get(
        "/api/usage-analytics/totals",
        params={"since": T0.isoformat(), "until": (T0 + timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["totals"]["cost_usd"] == "3.65"


def test_api_leaderboard() -> None:
    resp = _client().get(
        "/api/usage-analytics/leaderboard",
        params={"by": "model", "metric": "cost_usd",
                "since": T0.isoformat(), "until": (T0 + timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows[0]["key"]["model"] == "wan2.7-i2v"


def test_api_attribution_and_unit_economics() -> None:
    c = _client()
    qs = {"since": T0.isoformat(), "until": (T0 + timedelta(days=2)).isoformat()}
    bd = c.get("/api/usage-analytics/attribution", params=qs).json()
    assert bd["breakdown"]["total_usd"] == "3.65"
    ue = c.get("/api/usage-analytics/unit-economics", params=qs).json()
    assert ue["unit_economics"]["cost_per_finished_minute_usd"] == "7.300000"


def test_api_burndown() -> None:
    store = InMemoryUsageMetricStore()
    for i in range(10):
        store.record(_ev(at=T0 + timedelta(days=i), cost="2.00"))
    svc = UsageAnalyticsService(store=store)
    resp = _client(svc).get(
        "/api/usage-analytics/burndown",
        params={"as_of": (T0 + timedelta(days=9)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["will_exceed"] is True
    assert data["cap_usd"] == "30"


def test_api_anomalies() -> None:
    store = InMemoryUsageMetricStore()
    for i in range(3):
        store.record(_ev(at=T0 + timedelta(hours=i), cost="1.00"))
    store.record(_ev(at=T0 + timedelta(hours=3), cost="50.00"))
    svc = UsageAnalyticsService(store=store)
    resp = _client(svc).get(
        "/api/usage-analytics/anomalies",
        params={"since": T0.isoformat(), "until": (T0 + timedelta(hours=5)).isoformat(),
                "granularity": "hour"},
    )
    assert resp.status_code == 200, resp.text
    alerts = resp.json()["alerts"]
    assert any(a["kind"] == "spend_spike" for a in alerts)


def test_api_overview() -> None:
    resp = _client().get(
        "/api/usage-analytics/overview",
        params={"since": T0.isoformat(), "until": (T0 + timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "totals" in data and "burndown" in data and "top_models" in data


def test_api_disabled_returns_404() -> None:
    resp = _client(enabled=False).get("/api/usage-analytics/totals")
    assert resp.status_code == 404


def test_api_invalid_metric_422() -> None:
    resp = _client().get("/api/usage-analytics/series", params={"metric": "bogus"})
    assert resp.status_code == 422


def test_api_invalid_window_422() -> None:
    resp = _client().get(
        "/api/usage-analytics/totals",
        params={"since": (T0 + timedelta(days=2)).isoformat(), "until": T0.isoformat()},
    )
    assert resp.status_code == 422


def test_api_provider_filter() -> None:
    resp = _client().get(
        "/api/usage-analytics/totals",
        params={"since": T0.isoformat(), "until": (T0 + timedelta(days=2)).isoformat(),
                "provider": "wan"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["totals"]["cost_usd"] == "3.60"
