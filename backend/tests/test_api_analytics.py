"""Product-analytics API tests (kinora.md §13/§5).

Covers the batched/idempotent ingest endpoint and the query / engagement /
funnel / retention read endpoints. Infra-bound (the gateway needs throwaway
Postgres + Redis + MinIO for auth + rate-limiting); skips cleanly when absent,
like the rest of the gateway suite. The analytics *store* is overridden to the
deterministic in-memory implementation so these tests assert the route/contract
without depending on the ``analytics_events`` table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.analytics.store import InMemoryAnalyticsStore
from app.composition import Container

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _iso(minute: float = 0) -> str:
    return (BASE + timedelta(minutes=minute)).isoformat()


def _events_payload() -> dict:
    return {
        "events": [
            {"event_id": "e1", "name": "app.opened", "occurred_at": _iso(0)},
            {
                "event_id": "e2",
                "name": "book.opened",
                "occurred_at": _iso(1),
                "book_id": "b1",
            },
            {
                "event_id": "e3",
                "name": "reading.started",
                "occurred_at": _iso(2),
                "book_id": "b1",
            },
        ]
    }


async def test_ingest_is_idempotent(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.post(
        "/api/analytics/events", headers=auth_headers, json=_events_payload()
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["received"] == 3
    assert body["new"] == 3

    # Re-post the same batch -> nothing new (idempotent on event_id).
    resp2 = await api_client.post(
        "/api/analytics/events", headers=auth_headers, json=_events_payload()
    )
    assert resp2.json()["new"] == 0


async def test_ingest_rejects_unknown_event_name(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.post(
        "/api/analytics/events",
        headers=auth_headers,
        json={"events": [{"event_id": "x", "name": "bogus.event", "occurred_at": _iso()}]},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["new"] == 0
    assert body["rejected"] == 1


async def test_ingest_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.post("/api/analytics/events", json=_events_payload())
    assert resp.status_code == 401


async def test_query_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    await api_client.post("/api/analytics/events", headers=auth_headers, json=_events_payload())
    resp = await api_client.post(
        "/api/analytics/query",
        headers=auth_headers,
        json={
            "metric": "count",
            "granularity": "day",
            "since": _iso(-60),
            "until": _iso(60),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "count"
    total = sum(p["value"] for s in body["series"] for p in s["points"])
    assert total == 3.0


async def test_query_group_by_name(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    await api_client.post("/api/analytics/events", headers=auth_headers, json=_events_payload())
    resp = await api_client.post(
        "/api/analytics/query",
        headers=auth_headers,
        json={
            "metric": "count",
            "granularity": "day",
            "group_by": "name",
            "since": _iso(-60),
            "until": _iso(60),
        },
    )
    assert resp.status_code == 200
    groups = {s["group"] for s in resp.json()["series"]}
    assert "app.opened" in groups


async def test_query_invalid_metric(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.post(
        "/api/analytics/query",
        headers=auth_headers,
        json={"metric": "sum"},  # missing prop
    )
    assert resp.status_code == 422


async def test_engagement_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    payload = {
        "events": [
            {
                "event_id": "p1",
                "name": "page.viewed",
                "occurred_at": _iso(0),
                "book_id": "b1",
                "props": {"page": 0, "page_count": 4},
            },
            {
                "event_id": "p2",
                "name": "page.viewed",
                "occurred_at": _iso(5),
                "book_id": "b1",
                "props": {"page": 3, "page_count": 4},
            },
        ]
    }
    await api_client.post("/api/analytics/events", headers=auth_headers, json=payload)
    resp = await api_client.get(
        "/api/analytics/engagement",
        headers=auth_headers,
        params={"since": _iso(-60), "until": _iso(60)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_count"] == 1
    assert body["completion_rate"] == 1.0


async def test_funnel_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    await api_client.post("/api/analytics/events", headers=auth_headers, json=_events_payload())
    resp = await api_client.post(
        "/api/analytics/funnel",
        headers=auth_headers,
        json={
            "steps": ["app.opened", "book.opened", "reading.started"],
            "since": _iso(-60),
            "until": _iso(60),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_entered"] == 1
    assert body["total_converted"] == 1
    assert body["overall_conversion"] == 1.0


async def test_funnel_rejects_unknown_step(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.post(
        "/api/analytics/funnel",
        headers=auth_headers,
        json={"steps": ["app.opened", "not.real"]},
    )
    assert resp.status_code == 422


async def test_retention_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    await api_client.post("/api/analytics/events", headers=auth_headers, json=_events_payload())
    resp = await api_client.get(
        "/api/analytics/retention",
        headers=auth_headers,
        params={"granularity": "day", "max_offset": 1, "since": _iso(-60), "until": _iso(2880)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["granularity"] == "day"
    assert len(body["cohorts"]) >= 1


async def test_retention_rejects_month_granularity(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.get(
        "/api/analytics/retention",
        headers=auth_headers,
        params={"granularity": "month"},
    )
    assert resp.status_code == 422


async def test_query_invalid_window(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    container.analytics_store = InMemoryAnalyticsStore()
    resp = await api_client.post(
        "/api/analytics/query",
        headers=auth_headers,
        json={"metric": "count", "since": _iso(60), "until": _iso(0)},
    )
    assert resp.status_code == 422
