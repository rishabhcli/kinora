"""Smoke tests for the meta endpoints: root, liveness, readiness, metrics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.conftest import requires_infra


class _ReadinessContainer:
    """A minimal container double exposing only the readiness probe + lifecycle."""

    def __init__(self, *, postgres: bool = True, redis: bool = True) -> None:
        self._checks = {"postgres": postgres, "redis": redis}

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def check_readiness(self) -> dict[str, bool]:
        return dict(self._checks)


@asynccontextmanager
async def _ready_client(container: _ReadinessContainer) -> AsyncIterator[AsyncClient]:
    """An HTTP client over a fresh app with ``container`` injected (no infra)."""
    app = create_app()
    app.state.container = container
    app.state.run_idle_sweeper = False
    app.state.run_realtime_sweeper = False
    app.state.run_notification_bridge = False
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


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


async def test_ready_200_when_dependencies_up() -> None:
    async with _ready_client(_ReadinessContainer(postgres=True, redis=True)) as http:
        response = await http.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "kinora"
    assert body["uptime_seconds"] >= 0
    assert body["checks"] == {"postgres": True, "redis": True}


async def test_ready_503_when_a_dependency_is_down() -> None:
    # Redis down -> the readiness gate must fail closed with 503 (liveness stays ok).
    async with _ready_client(_ReadinessContainer(postgres=True, redis=False)) as http:
        response = await http.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"postgres": True, "redis": False}


@requires_infra
async def test_ready_probes_real_postgres_and_redis(api_client: AsyncClient) -> None:
    # Against the throwaway Postgres + Redis, the real probe (SELECT 1 + PING) passes.
    response = await api_client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": True, "redis": True}


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
