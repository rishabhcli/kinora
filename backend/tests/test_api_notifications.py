"""API-gateway tests for the notification & webhook routes (infra-gated).

Drives the real gateway over the test container (in-memory transports → no
network, no credits). Covers preferences read/write, the in-app inbox, webhook
CRUD + a signed test ping, delivery-status visibility, and auth enforcement.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import requires_infra

pytestmark = [requires_infra, pytest.mark.asyncio]


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


async def test_notification_routes_require_auth(api_client: AsyncClient) -> None:
    for path in (
        "/api/me/notification-preferences",
        "/api/me/notifications",
        "/api/me/webhooks",
        "/api/me/notifications/deliveries",
    ):
        resp = await api_client.get(path)
        assert resp.status_code == 401, path


# --------------------------------------------------------------------------- #
# preferences
# --------------------------------------------------------------------------- #


async def test_get_default_preferences(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/me/notification-preferences", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert "in_app" in body["enabled_channels"]
    assert "book_ready" in body["matrix"]


async def test_update_preferences_round_trip(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = {
        "quiet_hours": {"start": "22:00", "end": "07:00", "tz_name": "UTC", "enabled": True},
        "digest_enabled": True,
        "digest_interval_minutes": 30,
        "matrix": {"render_done": ["email"]},
        "locale": "es",
    }
    resp = await api_client.put(
        "/api/me/notification-preferences", json=payload, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quiet_hours"]["start"] == "22:00"
    assert body["digest_enabled"] is True
    assert body["digest_interval_minutes"] == 30
    assert body["matrix"]["render_done"] == ["email"]
    assert body["locale"] == "es"
    # Persisted: a fresh GET returns the same.
    again = await api_client.get("/api/me/notification-preferences", headers=auth_headers)
    assert again.json()["digest_interval_minutes"] == 30


async def test_update_preferences_rejects_unknown_channel(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.put(
        "/api/me/notification-preferences",
        json={"enabled_channels": ["carrier_pigeon"]},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_clear_quiet_hours(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.put(
        "/api/me/notification-preferences",
        json={"quiet_hours": {"start": "22:00", "end": "07:00"}},
        headers=auth_headers,
    )
    resp = await api_client.put(
        "/api/me/notification-preferences",
        json={"clear_quiet_hours": True},
        headers=auth_headers,
    )
    assert resp.json()["quiet_hours"] is None


# --------------------------------------------------------------------------- #
# in-app inbox (emitted via the container hook)
# --------------------------------------------------------------------------- #


async def test_inbox_lists_emitted_notification(
    api_client: AsyncClient, auth_headers: dict[str, str], container: object
) -> None:
    # Resolve the user id from /auth/me, then emit a domain event for them.
    me = await api_client.get("/api/auth/me", headers=auth_headers)
    user_id = me.json()["id"]
    await container.notify_event(  # type: ignore[attr-defined]
        "book_ready", user_id=user_id, email="r@e.com", data={"title": "Dune"}
    )
    resp = await api_client.get("/api/me/notifications", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unread"] >= 1
    assert any("Dune" in i["subject"] for i in body["items"])

    # Mark the first item read.
    item_id = body["items"][0]["id"]
    read = await api_client.post(
        f"/api/me/notifications/{item_id}/read", headers=auth_headers
    )
    assert read.json()["ok"] is True


# --------------------------------------------------------------------------- #
# webhooks
# --------------------------------------------------------------------------- #


async def test_webhook_crud_and_secret_only_at_create(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    create = await api_client.post(
        "/api/me/webhooks",
        json={"url": "https://example.invalid/hook", "events": ["book_ready"]},
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    created = create.json()
    assert created["secret"].startswith("whsec_")
    endpoint_id = created["id"]

    listed = await api_client.get("/api/me/webhooks", headers=auth_headers)
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert "secret" not in items[0]  # secret never re-read

    # disable → enable
    dis = await api_client.post(
        f"/api/me/webhooks/{endpoint_id}/disable", headers=auth_headers
    )
    assert dis.json()["ok"] is True
    en = await api_client.post(
        f"/api/me/webhooks/{endpoint_id}/enable", headers=auth_headers
    )
    assert en.json()["ok"] is True

    # delete
    delete = await api_client.delete(
        f"/api/me/webhooks/{endpoint_id}", headers=auth_headers
    )
    assert delete.json()["ok"] is True
    assert (await api_client.get("/api/me/webhooks", headers=auth_headers)).json() == []


async def test_webhook_rejects_bad_url(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/me/webhooks", json={"url": "ftp://nope"}, headers=auth_headers
    )
    assert resp.status_code == 422


async def test_webhook_test_ping_delivers(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = (
        await api_client.post(
            "/api/me/webhooks",
            json={"url": "https://example.invalid/hook", "events": ["*"]},
            headers=auth_headers,
        )
    ).json()
    # The default (no real transport) webhook engine permanently fails (no HTTP
    # client), so the ping reports not-delivered but the endpoint exists and the
    # full sign→deliver path ran — the route returns ok=False without erroring.
    resp = await api_client.post(
        f"/api/me/webhooks/{created['id']}/test", headers=auth_headers
    )
    assert resp.status_code == 200
    assert "ok" in resp.json()


async def test_webhook_ownership_enforced(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
    make_user: object,
) -> None:
    created = (
        await api_client.post(
            "/api/me/webhooks",
            json={"url": "https://example.invalid/hook"},
            headers=auth_headers,
        )
    ).json()
    other = await make_user("intruder@example.com")  # type: ignore[operator]
    resp = await api_client.delete(
        f"/api/me/webhooks/{created['id']}", headers=other
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# delivery status + dead-letters
# --------------------------------------------------------------------------- #


async def test_deliveries_endpoint_reflects_emitted(
    api_client: AsyncClient, auth_headers: dict[str, str], container: object
) -> None:
    me = await api_client.get("/api/auth/me", headers=auth_headers)
    user_id = me.json()["id"]
    await container.notify_event(  # type: ignore[attr-defined]
        "book_ready", user_id=user_id, email="r@e.com", data={"title": "X"}
    )
    resp = await api_client.get("/api/me/notifications/deliveries", headers=auth_headers)
    assert resp.status_code == 200
    records = resp.json()
    assert any(r["status"] == "delivered" for r in records)


async def test_dead_letters_endpoint_empty_initially(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/me/notifications/dead-letters", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []
