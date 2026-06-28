"""API integration tests for the search routes (/api/search*).

Exercises the full HTTP path against the throwaway infra (Postgres + Redis +
MinIO): register → create an owned book with pages/beats/shots → reindex →
search / suggest, plus the per-user scoping guards. The container's embedder is
the conftest ``FakeEmbedder`` (deterministic, no network, zero credits).

Skips cleanly without ``KINORA_TEST_DATABASE_URL`` / ``_REDIS_URL`` /
``_S3_ENDPOINT_URL`` (the ``requires_infra`` gate from conftest).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from app.composition import Container
from app.db.models.beat import Beat
from app.db.models.book import Page
from app.db.models.entity import Entity
from app.db.models.enums import EntityType, ShotStatus
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from tests.conftest import register_login, requires_infra, seed_owned_book, user_id_for

pytestmark = requires_infra


async def _seed_book_content(container: Container, book_id: str) -> None:
    """Add searchable rows (page/scene/beat/entity/shot) to an owned book."""
    async with container.session_factory() as session:
        session.add(
            Page(id=f"{book_id}-pg1", book_id=book_id, page_number=1,
                 text="Gerda walked through the frozen forest searching for Kai.")
        )
        session.add(
            Scene(id=f"{book_id}-sc1", book_id=book_id, scene_index=1,
                  title="The Ice Palace", page_start=1, page_end=3)
        )
        # Flush so the scene exists before the beat's FK references it (no
        # relationship() is defined to imply the insert order).
        await session.flush()
        session.add(
            Beat(id=f"{book_id}-bt1", book_id=book_id, scene_id=f"{book_id}-sc1",
                 beat_index=1, summary="Gerda enters the palace",
                 entities=["char_gerda"], described_visuals="frost on the walls")
        )
        session.add(
            Entity(id=f"{book_id}-e1", book_id=book_id, entity_key="char_gerda",
                   type=EntityType.CHARACTER, name="Gerda", aliases=["the girl"],
                   description="A brave young girl with red boots.",
                   version=1, valid_from_beat=1, valid_to_beat=None)
        )
        session.add(
            Shot(id=f"{book_id}-s1", book_id=book_id, scene_id=f"{book_id}-sc1",
                 beat_id=f"{book_id}-bt1", status=ShotStatus.ACCEPTED,
                 render_mode="reference_to_video",
                 prompt="Wide shot of the glittering ice gates", duration_s=5.0,
                 qa={"verdict": "pass", "score": 0.9})
        )


@pytest.fixture(autouse=True)
def _memory_search_backend(container: Container) -> None:
    """Use the in-memory search backend for the API tests (no FTS schema needed).

    The conftest container is built from ``build_test_settings`` (postgres
    backend by default); for the API surface tests we swap in the in-memory
    index so the test exercises the route/scoping logic without depending on the
    generated tsvector column being present in the ``create_all`` schema. The
    Postgres FTS path has dedicated coverage in test_search_postgres_integration.
    """
    from app.search.memory_backend import InMemoryIndex

    container.settings.search_backend = "memory"
    container.search_index = InMemoryIndex()


async def test_search_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/search", params={"q": "frost"})
    assert resp.status_code == 401


async def test_reindex_and_search(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="The Snow Queen")
    await _seed_book_content(container, book_id)

    # Reindex the owned book.
    r = await api_client.post(f"/api/search/reindex/{book_id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["indexed"] >= 5  # book + page + scene + beat + entity + shot
    assert "beat" in body["by_kind"]

    # Search free-text across the owned library.
    r = await api_client.get("/api/search", params={"q": "frozen forest"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] >= 1
    doc_ids = {h["doc_id"] for h in data["hits"]}
    assert f"page:{book_id}-pg1" in doc_ids


async def test_search_book_scope(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Scoped Book")
    await _seed_book_content(container, book_id)
    await api_client.post(f"/api/search/reindex/{book_id}", headers=auth_headers)

    r = await api_client.get(
        "/api/search",
        params={"q": "gerda", "book_id": book_id, "kind": "entity"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert all(h["kind"] == "entity" for h in data["hits"])
    assert all(h["book_id"] == book_id for h in data["hits"])


async def test_search_other_users_book_is_404(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Any],
) -> None:
    # Owner A creates + indexes a book.
    owner_a = auth_headers
    book_id = await seed_owned_book(api_client, container, owner_a, title="A's Book")
    await _seed_book_content(container, book_id)
    await api_client.post(f"/api/search/reindex/{book_id}", headers=owner_a)

    # User B asks to search A's book by id -> 404 (fail-closed ownership).
    headers_b = await make_user("userb@example.com")
    r = await api_client.get(
        "/api/search", params={"q": "gerda", "book_id": book_id}, headers=headers_b
    )
    assert r.status_code == 404


async def test_search_library_wide_excludes_other_users(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Any],
) -> None:
    # A indexes a book containing "gerda".
    book_a = await seed_owned_book(api_client, container, auth_headers, title="A's Book")
    await _seed_book_content(container, book_a)
    await api_client.post(f"/api/search/reindex/{book_a}", headers=auth_headers)

    # B does a library-wide search (no book_id) -> must not see A's content.
    headers_b = await make_user("userb2@example.com")
    r = await api_client.get("/api/search", params={"q": "gerda"}, headers=headers_b)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


async def test_suggest_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Suggest Book")
    await _seed_book_content(container, book_id)
    await api_client.post(f"/api/search/reindex/{book_id}", headers=auth_headers)

    r = await api_client.get("/api/search/suggest", params={"q": "fro"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["prefix"] == "fro"
    assert any(s.startswith("fro") for s in data["suggestions"])


async def test_search_invalid_mode_422(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await api_client.get(
        "/api/search", params={"q": "x", "mode": "nonsense"}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_reindex_other_users_book_404(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Any],
) -> None:
    book_a = await seed_owned_book(api_client, container, auth_headers, title="A reindex book")
    headers_b = await make_user("userb3@example.com")
    r = await api_client.post(f"/api/search/reindex/{book_a}", headers=headers_b)
    assert r.status_code == 404


async def test_search_user_with_no_books_returns_empty(
    api_client: AsyncClient, make_user: Callable[[str], Any]
) -> None:
    headers = await make_user("lonely@example.com")
    r = await api_client.get("/api/search", params={"q": "anything"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0


async def test_user_id_helper_smoke(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Sanity: the auth wiring the search routes depend on resolves a user id.
    uid = await user_id_for(api_client, auth_headers)
    assert uid
    # And register_login is idempotent (used by make_user).
    again = await register_login(api_client, "owner@example.com")
    assert "Authorization" in again
