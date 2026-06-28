"""API tests for the content-translation routes.

Uses the shared gateway fixtures (``api_client``, ``container``, ``auth_headers``),
which require the throwaway Postgres + Redis + MinIO and **skip cleanly** when
that infra is not configured. The translation provider is overridden with the
deterministic :class:`FakeTranslationProvider`, so no live model call is made.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.composition import Container
from app.translation.provider import FakeTranslationProvider
from tests.conftest import seed_owned_book

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _inject_fake_provider(container: Container) -> None:
    """Override the translation provider so API tests never call a live model."""
    container.translation_provider = FakeTranslationProvider()


async def test_list_languages(api_client: AsyncClient, auth_headers: dict[str, str]) -> None:
    resp = await api_client.get("/api/translation/languages", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    langs = resp.json()["languages"]
    tags = {lang["tag"] for lang in langs}
    assert "fr" in tags and "ar" in tags
    ar = next(lang for lang in langs if lang["tag"] == "ar")
    assert ar["rtl"] is True and ar["direction"] == "rtl"


async def test_languages_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/translation/languages")
    assert resp.status_code == 401


async def test_translate_persists_and_caches(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    body = {
        "target_lang": "fr",
        "source_lang": "en",
        "segments": [
            {"id": "p1.0", "text": "The cat sat on the mat.", "kind": "page_text"},
            {"id": "p1.1", "text": "See <b>{name}</b> now.", "kind": "narration"},
        ],
    }
    resp = await api_client.post(
        f"/api/books/{book_id}/translate", json=body, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["target_lang"] == "fr"
    assert len(data["segments"]) == 2
    # Markup survived.
    narr = next(s for s in data["segments"] if s["id"] == "p1.1")
    assert "<b>{name}</b>" in narr["translated_text"]
    assert data["cost"]["cache_hits"] == 0

    # Second identical call → served from the persisted cache (zero provider work).
    resp2 = await api_client.post(
        f"/api/books/{book_id}/translate", json=body, headers=auth_headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["cost"]["cache_hits"] == 2
    assert resp2.json()["cost"]["provider_calls"] == 0


async def test_translate_unknown_language_400(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/translate",
        json={"target_lang": "klingon", "segments": [{"id": "a", "text": "hi"}]},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "unknown_language"


async def test_translate_foreign_book_404(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    other = await make_user("intruder@example.com")
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/translate",
        json={"target_lang": "fr", "segments": [{"id": "a", "text": "hi"}]},
        headers=other,
    )
    assert resp.status_code == 404


async def test_list_and_get_translations(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await api_client.post(
        f"/api/books/{book_id}/translate",
        json={
            "target_lang": "es",
            "source_lang": "en",
            "segments": [{"id": "s0", "text": "Hello world."}],
        },
        headers=auth_headers,
    )
    listing = await api_client.get(f"/api/books/{book_id}/translations", headers=auth_headers)
    assert listing.status_code == 200
    artifacts = listing.json()["artifacts"]
    assert any(a["target_lang"] == "es" for a in artifacts)

    detail = await api_client.get(
        f"/api/books/{book_id}/translations/es/page_text", headers=auth_headers
    )
    assert detail.status_code == 200
    assert detail.json()["segments"][0]["segment_id"] == "s0"


async def test_glossary_crud_and_effect(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    # Add a DNT term.
    add = await api_client.post(
        f"/api/books/{book_id}/glossary",
        json={"source_term": "Elsa", "do_not_translate": True},
        headers=auth_headers,
    )
    assert add.status_code == 200, add.text
    listing = await api_client.get(f"/api/books/{book_id}/glossary", headers=auth_headers)
    assert any(e["source_term"] == "Elsa" for e in listing.json()["entries"])

    # Translating now keeps "Elsa" verbatim.
    resp = await api_client.post(
        f"/api/books/{book_id}/translate",
        json={
            "target_lang": "fr",
            "source_lang": "en",
            "segments": [{"id": "s0", "text": "Elsa walked away."}],
        },
        headers=auth_headers,
    )
    assert "Elsa" in resp.json()["segments"][0]["translated_text"]


async def test_glossary_requires_dnt_or_targets(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/glossary",
        json={"source_term": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_review_workflow_via_api(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    # Use a markup-corrupting provider so a segment is flagged for review.
    container.translation_provider = FakeTranslationProvider(corrupt_markup=True)
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await api_client.post(
        f"/api/books/{book_id}/translate",
        json={
            "target_lang": "fr",
            "source_lang": "en",
            "segments": [{"id": "s0", "text": "Hello {name} and {place}."}],
        },
        headers=auth_headers,
    )
    reviews = await api_client.get(f"/api/books/{book_id}/reviews", headers=auth_headers)
    assert reviews.status_code == 200
    items = reviews.json()["reviews"]
    assert len(items) >= 1
    review_id = items[0]["id"]

    # Edit it via the API.
    edit = await api_client.post(
        f"/api/reviews/{review_id}/edit",
        json={"edited_text": "Bonjour {name} et {place}."},
        headers=auth_headers,
    )
    assert edit.status_code == 200, edit.text
    assert edit.json()["status"] == "edited"

    # Filter pending reviews → none open now.
    pending = await api_client.get(
        f"/api/books/{book_id}/reviews?status=pending", headers=auth_headers
    )
    assert pending.json()["reviews"] == []


async def test_review_action_on_foreign_book_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/reviews/nonexistent/approve", json={}, headers=auth_headers
    )
    assert resp.status_code == 404
