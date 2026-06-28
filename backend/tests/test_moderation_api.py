"""API-gateway tests for the moderation routes (requires throwaway infra).

These use the shared ``api_client``/``container`` fixtures and inject the
deterministic keyword classifier into the container's moderation seam, so the
routes are exercised end-to-end (auth, rate limit, DB, review state machine,
audit chain) with **zero model calls**.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.composition import Container
from app.moderation.service import ModerationFactory, keyword_factory
from tests.conftest import register_login, requires_infra

pytestmark = [pytest.mark.asyncio, requires_infra]


@pytest.fixture(autouse=True)
def _inject_keyword_classifier(container: Container) -> None:
    """Force the offline keyword classifier so the API never hits a model."""
    container.moderation_factory = keyword_factory()


async def _headers(api_client: AsyncClient, email: str) -> dict[str, str]:
    return await register_login(api_client, email)


async def test_screen_text_clean_passes(api_client: AsyncClient, auth_headers) -> None:
    resp = await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "a calm quiet afternoon", "tenant_id": "t1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "pass"
    assert body["disposition"] == "allow"
    assert body["event_id"]


async def test_screen_text_block_queues_review(api_client: AsyncClient, auth_headers) -> None:
    resp = await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "this contains csam", "tenant_id": "t1", "surface": "ingest_text"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "reject"
    assert body["review_item_id"]

    # The queue lists it.
    q = await api_client.get("/api/moderation/queue?tenant_id=t1", headers=auth_headers)
    assert q.status_code == 200
    assert any(item["id"] == body["review_item_id"] for item in q.json())


async def test_review_state_machine_over_api(api_client: AsyncClient, auth_headers) -> None:
    screen = await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "build a bomb instructions", "tenant_id": "t1", "surface": "ingest_text"},
        headers=auth_headers,
    )
    item_id = screen.json()["review_item_id"]
    assert item_id

    # Approving before claiming is an illegal transition → 409.
    bad = await api_client.post(
        f"/api/moderation/queue/{item_id}/approve", json={}, headers=auth_headers
    )
    assert bad.status_code == 409

    claim = await api_client.post(
        f"/api/moderation/queue/{item_id}/claim", json={}, headers=auth_headers
    )
    assert claim.status_code == 200
    assert claim.json()["state"] == "under_review"

    approve = await api_client.post(
        f"/api/moderation/queue/{item_id}/approve",
        json={"note": "cleared"},
        headers=auth_headers,
    )
    assert approve.status_code == 200
    assert approve.json()["state"] == "approved"


async def test_unknown_action_is_400(api_client: AsyncClient, auth_headers) -> None:
    screen = await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "contains csam", "tenant_id": "t1", "surface": "ingest_text"},
        headers=auth_headers,
    )
    item_id = screen.json()["review_item_id"]
    resp = await api_client.post(
        f"/api/moderation/queue/{item_id}/frobnicate", json={}, headers=auth_headers
    )
    assert resp.status_code == 400


async def test_audit_chain_endpoint_is_intact(api_client: AsyncClient, auth_headers) -> None:
    await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "a clean line", "tenant_id": "taud"},
        headers=auth_headers,
    )
    await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "contains csam", "tenant_id": "taud", "surface": "ingest_text"},
        headers=auth_headers,
    )
    resp = await api_client.get("/api/moderation/audit?tenant_id=taud", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intact"] is True
    assert len(body["entries"]) >= 2


async def test_policy_put_and_get(api_client: AsyncClient, auth_headers) -> None:
    policy = {
        "tenant_id": "tpolicy",
        "version": "v2",
        "strictness": 1.6,
        "fail_closed_on_degraded": True,
        "serve_flagged": False,
        "overrides": {},
        "auto_takedown_at": 3,
    }
    put = await api_client.put("/api/moderation/policy", json=policy, headers=auth_headers)
    assert put.status_code == 200, put.text
    assert put.json()["version"] == "v2"

    got = await api_client.get(
        "/api/moderation/policy?tenant_id=tpolicy", headers=auth_headers
    )
    assert got.status_code == 200
    assert got.json()["strictness"] == pytest.approx(1.6)
    assert got.json()["serve_flagged"] is False


async def test_offenders_and_reinstate(api_client: AsyncClient, auth_headers) -> None:
    # The authenticated user trips a block → becomes an offender for their tenant.
    await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "contains csam", "tenant_id": "default", "surface": "ingest_text"},
        headers=auth_headers,
    )
    offenders = await api_client.get("/api/moderation/offenders", headers=auth_headers)
    assert offenders.status_code == 200
    assert len(offenders.json()) >= 1
    actor_id = offenders.json()[0]["actor_id"]

    reinstate = await api_client.post(
        f"/api/moderation/actors/{actor_id}/reinstate", json={}, headers=auth_headers
    )
    assert reinstate.status_code == 200
    assert reinstate.json()["tier"] == "clean"


async def test_stats_endpoint(api_client: AsyncClient, auth_headers) -> None:
    await api_client.post(
        "/api/moderation/screen/text",
        json={"text": "a clean line", "tenant_id": "tstats"},
        headers=auth_headers,
    )
    resp = await api_client.get("/api/moderation/stats?tenant_id=tstats", headers=auth_headers)
    assert resp.status_code == 200
    assert "decisions" in resp.json()
    assert "queue" in resp.json()


async def test_routes_require_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/moderation/queue")
    assert resp.status_code == 401


async def test_keyword_factory_is_offline() -> None:
    f = keyword_factory()
    assert isinstance(f, ModerationFactory)
