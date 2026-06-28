"""API-route tests for /api/integrations (gated on throwaway infra).

Drives the HTTP surface end-to-end through the FastAPI gateway, with the
container's integrations facade overridden to use a fake HTTP client + fake
ingest gateway (no network, no DashScope). Skips when infra is unset.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.composition import Container
from app.integrations.clock import FakeClock
from app.integrations.crypto import TokenSealer
from app.integrations.http import FakeHttpClient
from app.integrations.service import IntegrationsService
from tests.conftest import requires_infra
from tests.test_integrations_service import FakeIngestGateway

pytestmark = requires_infra


@pytest.fixture
def fake_http() -> FakeHttpClient:
    return FakeHttpClient()


@pytest_asyncio.fixture
async def wired_container(container: Container, fake_http: FakeHttpClient) -> Container:
    """Override the container's integrations facade with offline seams."""
    container.integrations = IntegrationsService(
        session_factory=container.session_factory,
        ingest=FakeIngestGateway(container),
        http=fake_http,
        sealer=TokenSealer(key="test-key"),
        clock=FakeClock(),
    )
    return container


@pytest_asyncio.fixture
async def api(wired_container: Container) -> AsyncIterator[AsyncClient]:
    """An HTTP client over the gateway with the wired (offline) container."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport

    from app.main import create_app

    app = create_app()
    app.state.container = wired_container
    app.state.run_idle_sweeper = False
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def _auth(api: AsyncClient, email: str) -> dict[str, str]:
    await api.post("/api/auth/register", json={"email": email, "password": "password123"})
    resp = await api.post("/api/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_list_providers_requires_auth(api: AsyncClient) -> None:
    assert (await api.get("/api/integrations/providers")).status_code == 401


@pytest.mark.asyncio
async def test_list_providers(api: AsyncClient) -> None:
    headers = await _auth(api, "prov@example.com")
    resp = await api.get("/api/integrations/providers", headers=headers)
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert {"readwise", "kindle", "notion", "rss", "pocket", "web"} <= names
    readwise = next(p for p in resp.json() if p["name"] == "readwise")
    assert "incremental" in readwise["capabilities"]


@pytest.mark.asyncio
async def test_connect_sync_and_health_flow(
    api: AsyncClient, fake_http: FakeHttpClient
) -> None:
    headers = await _auth(api, "flow@example.com")
    fake_http.json_response(
        "GET",
        "/export",
        {
            "results": [
                {"user_book_id": 1, "title": "API Book",
                 "highlights": [{"text": "A line worth keeping forever.", "location": 1}]}
            ],
            "nextPageCursor": None,
        },
    )

    # Connect Readwise with a token.
    connect = await api.post(
        "/api/integrations/connections",
        headers=headers,
        json={"provider": "readwise", "token": "rw-tok"},
    )
    assert connect.status_code == 201, connect.text
    conn_id = connect.json()["id"]
    assert connect.json()["status"] == "active"

    # Sync now.
    sync = await api.post(f"/api/integrations/connections/{conn_id}/sync", headers=headers)
    assert sync.status_code == 200, sync.text
    assert sync.json()["imported"] == 1 and sync.json()["status"] == "success"

    # Connection health shows the import + a run.
    detail = await api.get(f"/api/integrations/connections/{conn_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["imported_count"] == 1 and body["health"] == "healthy"
    assert body["recent_runs"] and body["recent_runs"][0]["imported"] == 1

    # And it shows in the list.
    listing = await api.get("/api/integrations/connections", headers=headers)
    assert any(c["id"] == conn_id for c in listing.json())


@pytest.mark.asyncio
async def test_import_file_route(api: AsyncClient) -> None:
    headers = await _auth(api, "file@example.com")
    clippings = "Walden (Thoreau)\n- Highlight\n\nI went to the woods.\n==========\n"
    resp = await api.post(
        "/api/integrations/import/file",
        headers=headers,
        data={"provider": "kindle"},
        files={"file": ("My Clippings.txt", clippings.encode(), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1


@pytest.mark.asyncio
async def test_connect_unknown_provider_is_400(api: AsyncClient) -> None:
    headers = await _auth(api, "bad@example.com")
    resp = await api.post(
        "/api/integrations/connections",
        headers=headers,
        json={"provider": "myspace", "token": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "integration_misconfigured"


@pytest.mark.asyncio
async def test_disconnect_route(api: AsyncClient) -> None:
    headers = await _auth(api, "disc@example.com")
    connect = await api.post(
        "/api/integrations/connections",
        headers=headers,
        json={"provider": "rss", "config": {"feed_url": "http://x/feed"}},
    )
    conn_id = connect.json()["id"]
    resp = await api.delete(f"/api/integrations/connections/{conn_id}", headers=headers)
    assert resp.status_code == 204
    # Now hidden from the default listing.
    listing = await api.get("/api/integrations/connections", headers=headers)
    assert all(c["id"] != conn_id for c in listing.json())


@pytest.mark.asyncio
async def test_cross_user_cannot_see_connection(api: AsyncClient) -> None:
    owner = await _auth(api, "owner-api@example.com")
    connect = await api.post(
        "/api/integrations/connections",
        headers=owner,
        json={"provider": "readwise", "token": "t"},
    )
    conn_id = connect.json()["id"]
    other = await _auth(api, "other-api@example.com")
    resp = await api.get(f"/api/integrations/connections/{conn_id}", headers=other)
    assert resp.status_code == 400  # not found for this user (mapped to misconfigured)


@pytest.mark.asyncio
async def test_webhook_unverified_rejected(api: AsyncClient) -> None:
    # No webhook secret configured for this provider in test settings → 401.
    resp = await api.post(
        "/api/integrations/webhooks/readwise",
        content=b'{"event":"new"}',
        headers={"x-signature": "deadbeef"},
    )
    assert resp.status_code == 401
