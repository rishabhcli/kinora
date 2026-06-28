"""Compliance API endpoint tests (kinora.md §8/§11 governance surface).

Drives the mounted ``/api/compliance`` router through the gateway: consent
grant/withdraw + history, DSAR filing/cancel, the retention schedule, the
compliance report, and the subject ledger slice. Infra-bound (throwaway Postgres
+ Redis + MinIO via the shared ``api_client``/``auth_headers`` fixtures); skips
when that infra is absent, like the rest of the gateway suite.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from tests.conftest import requires_infra

pytestmark = requires_infra


async def test_consent_snapshot_lists_all_purposes(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/compliance/consent", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    purposes = {p["purpose"] for p in body["purposes"]}
    assert "adaptation" in purposes and "model_training" in purposes
    # Untouched purposes default to NEVER / not granted.
    assert all(p["state"] == "never" for p in body["purposes"])


async def test_grant_then_withdraw_roundtrip(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    grant = await api_client.post(
        "/api/compliance/consent/grant",
        headers=auth_headers,
        json={"purpose": "analytics"},
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["state"] == "granted"
    assert grant.json()["is_granted"] is True

    withdraw = await api_client.post(
        "/api/compliance/consent/withdraw",
        headers=auth_headers,
        json={"purpose": "analytics", "note": "changed my mind"},
    )
    assert withdraw.status_code == 200, withdraw.text
    assert withdraw.json()["state"] == "withdrawn"


async def test_consent_history_is_ordered_proof(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.post(
        "/api/compliance/consent/grant", headers=auth_headers, json={"purpose": "marketing_email"}
    )
    await api_client.post(
        "/api/compliance/consent/withdraw",
        headers=auth_headers,
        json={"purpose": "marketing_email"},
    )
    resp = await api_client.get("/api/compliance/consent/history", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    actions = [
        r["action"]
        for r in resp.json()["records"]
        if r["purpose"] == "marketing_email"
    ]
    assert actions == ["grant", "withdraw"]


async def test_dsar_open_and_cancel(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    opened = await api_client.post(
        "/api/compliance/dsar", headers=auth_headers, json={"kind": "access"}
    )
    assert opened.status_code == 200, opened.text
    req = opened.json()
    assert req["state"] == "received"
    assert req["kind"] == "access"
    assert req["due_at"] > req["received_at"]  # one-month deadline in the future

    listed = await api_client.get("/api/compliance/dsar", headers=auth_headers)
    assert any(r["id"] == req["id"] for r in listed.json()["requests"])

    cancelled = await api_client.post(
        f"/api/compliance/dsar/{req['id']}/cancel", headers=auth_headers
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"


async def test_dsar_cancel_other_users_request_404(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    opened = await api_client.post(
        "/api/compliance/dsar", headers=auth_headers, json={"kind": "erasure"}
    )
    req_id = opened.json()["id"]
    other = await make_user("intruder@example.com")
    resp = await api_client.post(f"/api/compliance/dsar/{req_id}/cancel", headers=other)
    assert resp.status_code == 404


async def test_retention_schedule_lists_rules(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/compliance/retention", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    classes = {r["data_class"] for r in resp.json()["rules"]}
    assert {"account", "uploaded_book", "audit_log", "billing_record"} <= classes


async def test_report_denies_then_allows(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Fresh user: adaptation is required but not granted → not compliant.
    first = await api_client.get("/api/compliance/report", headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["is_compliant"] is False

    await api_client.post(
        "/api/compliance/consent/grant", headers=auth_headers, json={"purpose": "adaptation"}
    )
    await api_client.post(
        "/api/compliance/consent/grant", headers=auth_headers, json={"purpose": "model_training"}
    )
    after = await api_client.get("/api/compliance/report", headers=auth_headers)
    assert after.json()["is_compliant"] is True
    assert after.json()["decision"] == "allow"


async def test_ledger_slice_records_consent_events(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.post(
        "/api/compliance/consent/grant", headers=auth_headers, json={"purpose": "analytics"}
    )
    resp = await api_client.get("/api/compliance/ledger", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert any(e["event"] == "consent.granted" for e in entries)
    # The chain is sequenced and each entry carries a hash.
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs)
    assert all(len(e["entry_hash"]) == 64 for e in entries)


async def test_admin_surface_forbidden_for_normal_user(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/compliance/holds",
        headers=auth_headers,
        json={"subject_id": "u1", "matter_id": "M-1", "reason": "x"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


async def test_endpoints_require_auth(api_client: AsyncClient) -> None:
    for method, path in (
        ("get", "/api/compliance/consent"),
        ("get", "/api/compliance/dsar"),
        ("get", "/api/compliance/report"),
        ("get", "/api/compliance/ledger"),
    ):
        resp = await getattr(api_client, method)(path)
        assert resp.status_code == 401, f"{path}: {resp.text}"
