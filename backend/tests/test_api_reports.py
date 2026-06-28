"""Integration tests for the reports API (require the throwaway infra).

These exercise the full HTTP path: generate → store (MinIO) → index (Postgres) →
list → retrieve → download. They use the same fixtures as the other gateway
tests and skip cleanly when ``KINORA_TEST_DATABASE_URL`` / ``_REDIS_URL`` /
``_S3_ENDPOINT_URL`` are unset.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.composition import Container
from tests.conftest import seed_owned_book

pytestmark = pytest.mark.asyncio


async def test_generate_reading_progress_pdf_round_trip(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Progress Book")
    resp = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "reading_progress", "format": "pdf", "book_id": book_id},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "reading_progress"
    assert body["audience"] == "reader"
    assert body["format"] == "pdf"
    assert body["status"] == "ready"
    assert body["download_url"]
    artifact_id = body["id"]

    # It appears in the caller's list.
    listed = await api_client.get("/api/reports", headers=auth_headers)
    assert listed.status_code == 200
    assert any(a["id"] == artifact_id for a in listed.json())

    # Metadata fetch returns a fresh signed URL.
    meta = await api_client.get(f"/api/reports/{artifact_id}", headers=auth_headers)
    assert meta.status_code == 200
    assert meta.json()["download_url"]

    # Download streams real PDF bytes.
    dl = await api_client.get(f"/api/reports/{artifact_id}/download", headers=auth_headers)
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/pdf")
    assert dl.content[:5] == b"%PDF-"


async def test_generate_dedups_identical_content(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    payload = {"kind": "reading_progress", "format": "json", "book_id": book_id}
    first = await api_client.post("/api/reports", headers=auth_headers, json=payload)
    second = await api_client.post("/api/reports", headers=auth_headers, json=payload)
    assert first.status_code == 201 and second.status_code == 201
    # Same content hash ⇒ deduped to the same artifact id.
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert first.json()["id"] == second.json()["id"]


async def test_preview_returns_model_json_without_storing(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.get(
        "/api/reports/preview",
        headers=auth_headers,
        params={"kind": "reading_progress", "book_id": book_id},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["meta"]["kind"] == "reading_progress"
    assert "sections" in data
    # Nothing was persisted by a preview.
    listed = await api_client.get("/api/reports", headers=auth_headers)
    assert listed.json() == []


async def test_reading_progress_requires_owned_book(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "reading_progress", "format": "pdf", "book_id": "does-not-exist"},
    )
    assert resp.status_code == 404


async def test_completion_certificate_rejects_incomplete_book(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    # A freshly-seeded book has no reading progress, so it is not complete.
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "completion_certificate", "format": "pdf", "book_id": book_id},
    )
    assert resp.status_code == 400
    assert "not complete" in resp.json()["error"]["message"]


async def test_operator_budget_report_in_local_env(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Test settings run app_env=local ⇒ any authed user is a report operator.
    resp = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "budget", "format": "html"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["audience"] == "operator"


async def test_operator_library_overview_pdf(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "library_overview", "format": "pdf"},
    )
    assert resp.status_code == 201, resp.text
    dl = await api_client.get(
        f"/api/reports/{resp.json()['id']}/download", headers=auth_headers
    )
    assert dl.status_code == 200
    assert dl.content[:5] == b"%PDF-"


async def test_list_filters_by_kind(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "reading_progress", "format": "json", "book_id": book_id},
    )
    await api_client.post(
        "/api/reports", headers=auth_headers, json={"kind": "budget", "format": "json"}
    )
    only_reader = await api_client.get(
        "/api/reports", headers=auth_headers, params={"kind": "reading_progress"}
    )
    assert only_reader.status_code == 200
    kinds = {a["kind"] for a in only_reader.json()}
    assert kinds == {"reading_progress"}


async def test_cannot_read_another_users_report(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user,
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    mine = await api_client.post(
        "/api/reports",
        headers=auth_headers,
        json={"kind": "reading_progress", "format": "json", "book_id": book_id},
    )
    artifact_id = mine.json()["id"]
    other = await make_user("intruder@example.com")
    resp = await api_client.get(f"/api/reports/{artifact_id}", headers=other)
    assert resp.status_code == 404


async def test_generate_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/api/reports", json={"kind": "budget", "format": "json"}
    )
    assert resp.status_code == 401
