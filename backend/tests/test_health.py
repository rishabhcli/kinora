"""Smoke tests for the meta endpoints: root, liveness, readiness, metrics."""

from __future__ import annotations

from httpx import AsyncClient


async def test_root_lists_endpoints(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "kinora"
    assert body["version"]
    assert body["docs"] == "/docs"
    assert body["endpoints"]["health"] == "/health"
    assert body["endpoints"]["metrics"] == "/metrics"


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "kinora"
    assert body["version"]
    assert body["environment"]
    assert body["uptime_seconds"] >= 0


async def test_ready_ok(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "kinora"
    assert body["uptime_seconds"] >= 0


async def test_metrics_exposition(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # The static app-info series is always present.
    assert "kinora_app_info" in response.text


async def test_metrics_path_label_is_bounded(client: AsyncClient) -> None:
    # A matched route is labelled by its template (low, fixed cardinality)...
    await client.get("/health")
    # ...while an unknown path must NOT mint its own series (no cardinality blow-up).
    await client.get("/no-such-route-1234567890")
    metrics = (await client.get("/metrics")).text
    assert 'path="/health"' in metrics
    assert 'path="<unmatched>"' in metrics
    assert "no-such-route-1234567890" not in metrics
