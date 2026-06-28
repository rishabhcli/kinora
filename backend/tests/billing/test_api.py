"""Integration tests for the billing API routes (isolated infra + fake provider).

These need the full gateway stack (Postgres + Redis + MinIO) because the routes
go through auth (JWT) and the wired Container. They reuse the root conftest's
``api_client`` / ``container`` fixtures (which point at the throwaway infra and
skip cleanly when it is absent), and additionally truncate the billing tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from app.billing.provider.fake import FakePaymentProvider
from app.composition import Container
from tests.conftest import register_login, requires_infra

pytestmark = requires_infra


@pytest_asyncio.fixture(autouse=True)
async def _truncate_billing(container: Container) -> AsyncIterator[None]:
    """Clean the billing tables on the shared throwaway DB before each test."""
    from app.db.base import Base

    names = ", ".join(
        f'"{t.name}"' for t in Base.metadata.sorted_tables if t.name.startswith("billing_")
    )
    if names:
        async with container.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    # Seed the catalog once per test so /plans + subscribe work.
    await container.billing_service.seed_catalog()  # type: ignore[attr-defined]
    yield


@pytest.fixture
def fake_provider(container: Container) -> FakePaymentProvider:
    """Inject a fresh fake provider so a test can script payment failures."""
    provider = FakePaymentProvider()
    container.billing_provider = provider
    container._billing_service = None  # rebuild the service against this provider
    return provider


async def test_list_plans(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/billing/plans")
    assert resp.status_code == 200, resp.text
    codes = {p["code"] for p in resp.json()["plans"]}
    assert {"free", "starter", "pro", "studio"} <= codes


async def test_subscribe_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.post("/api/billing/subscription", json={"plan_code": "pro"})
    assert resp.status_code == 401


async def test_subscribe_and_entitlements(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "sub@example.com")
    resp = await api_client.post(
        "/api/billing/subscription", json={"plan_code": "pro"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    sub = resp.json()
    assert sub["status"] == "trialing"

    ent = await api_client.get("/api/billing/entitlements", headers=headers)
    assert ent.status_code == 200
    body = ent.json()
    assert body["tier"] == "pro"
    assert "director_mode" in body["features"]
    assert body["allowances"]["render_seconds"] == 1200


async def test_free_plan_entitlements_gate(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "free@example.com")
    await api_client.post("/api/billing/subscription", json={"plan_code": "free"}, headers=headers)
    ent = (await api_client.get("/api/billing/entitlements", headers=headers)).json()
    # director_mode present as the locked sentinel (limit 0) on Free.
    assert ent["features"].get("director_mode") == 0


async def test_unknown_plan_404(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "x@example.com")
    resp = await api_client.post(
        "/api/billing/subscription", json={"plan_code": "nope"}, headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "plan_not_found"


async def test_record_usage_and_summary(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "usage@example.com")
    sub = (
        await api_client.post(
            "/api/billing/subscription", json={"plan_code": "pro"}, headers=headers
        )
    ).json()
    rec = await api_client.post(
        "/api/billing/usage",
        json={"meter": "render_seconds", "quantity": 42, "idempotency_key": "shot_x"},
        headers=headers,
    )
    assert rec.status_code == 202, rec.text
    assert rec.json()["recorded"] is True
    # Idempotent re-report.
    rec2 = await api_client.post(
        "/api/billing/usage",
        json={"meter": "render_seconds", "quantity": 42, "idempotency_key": "shot_x"},
        headers=headers,
    )
    assert rec2.json()["recorded"] is False

    usage = await api_client.get(f"/api/billing/subscription/{sub['id']}/usage", headers=headers)
    assert usage.status_code == 200
    assert usage.json()["by_meter"]["render_seconds"] == 42


async def test_change_plan_proration(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "change@example.com")
    sub = (
        await api_client.post(
            "/api/billing/subscription", json={"plan_code": "starter"}, headers=headers
        )
    ).json()
    resp = await api_client.post(
        f"/api/billing/subscription/{sub['id']}/change",
        json={"new_plan_code": "pro"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_cancel_subscription(api_client: AsyncClient) -> None:
    headers = await register_login(api_client, "cancel@example.com")
    sub = (
        await api_client.post(
            "/api/billing/subscription", json={"plan_code": "pro"}, headers=headers
        )
    ).json()
    resp = await api_client.post(
        f"/api/billing/subscription/{sub['id']}/cancel",
        json={"at_period_end": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["cancel_at_period_end"] is True


async def test_change_plan_ownership_enforced(api_client: AsyncClient) -> None:
    owner = await register_login(api_client, "owner-b@example.com")
    sub = (
        await api_client.post("/api/billing/subscription", json={"plan_code": "pro"}, headers=owner)
    ).json()
    intruder = await register_login(api_client, "intruder@example.com")
    resp = await api_client.post(
        f"/api/billing/subscription/{sub['id']}/change",
        json={"new_plan_code": "studio"},
        headers=intruder,
    )
    assert resp.status_code == 404  # fail-closed: not your subscription


async def test_webhook_bad_signature_400(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/api/billing/webhook",
        content=b'{"id":"evt_1","type":"invoice.payment_succeeded"}',
        headers={"x-billing-signature": "t=1,v1=bad"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "webhook_invalid_signature"


async def test_webhook_signed_roundtrip(api_client: AsyncClient, container: Container) -> None:
    # Build a correctly-signed webhook with the container's configured secret.
    from app.billing.provider.signing import build_signature_header

    secret = container.settings.billing_webhook_secret
    body = b'{"id":"evt_api_1","type":"customer.discount.created","created":0,"data":{}}'
    # Use a current timestamp so the tolerance window passes.
    import time

    header = build_signature_header(body, secret, timestamp=int(time.time()))
    resp = await api_client.post(
        "/api/billing/webhook", content=body, headers={"x-billing-signature": header}
    )
    assert resp.status_code == 200, resp.text
    # Unknown event type is accepted (verified) but ignored.
    assert resp.json()["status"] in ("ignored", "applied")
