"""Directing-style preference endpoint tests (kinora.md §8.6).

Covers the user-facing half of the cross-session loop: a Director comment / canon
edit teaches a prior, ``GET /…/prefs`` surfaces it in plain language, and
``DELETE /…/prefs`` resets it. Infra-bound (throwaway Postgres + Redis + MinIO);
skips when that infra is absent, like the rest of the gateway suite.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from httpx import AsyncClient

from app.composition import Container
from app.db.models.enums import EntityType, ShotStatus
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from tests.conftest import seed_owned_book


async def _create_session(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    resp = await client.post("/api/sessions", headers=headers, json={"book_id": book_id})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_id"])


async def _seed_shot(container: Container, book_id: str, shot_id: str) -> None:
    async with container.session_factory() as session:
        await ShotRepo(session).create(
            id=shot_id,
            book_id=book_id,
            beat_id=f"beat_{shot_id}",
            scene_id="scene_1",
            status=ShotStatus.ACCEPTED,
            render_mode="reference_to_video",
            seed=7,
            duration_s=5.0,
            reference_image_ids=["char_hero@v1"],
            canon_version_at_render=1,
        )


async def _comment(
    client: AsyncClient, headers: dict[str, str], session_id: str, shot_id: str, note: str
) -> None:
    resp = await client.post(
        f"/api/sessions/{session_id}/comment",
        headers=headers,
        json={"shot_id": shot_id, "note": note, "region_png": None},
    )
    assert resp.status_code == 200, resp.text


async def test_comment_learns_book_pacing_prior_and_surfaces_in_settings(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_p")
    session_id = await _create_session(api_client, auth_headers, book_id)

    # Session 1: three "slower" notes (the acceptance scenario).
    for _ in range(3):
        await _comment(
            api_client, auth_headers, session_id, "shot_p", "this is too fast — slow it down"
        )

    # The book's directing style now shows an applied "slower" pacing prior.
    resp = await api_client.get(f"/api/books/{book_id}/prefs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "book"
    pacing = next(p for p in body["priors"] if p["kind"] == "pacing")
    assert pacing["bias"] == -0.9
    assert pacing["applied"] is True
    assert pacing["applied_value"] == "slow"
    assert pacing["label"] == "You prefer slower, lingering shots"

    # The same prior rolls up into the reader's global directing style.
    resp = await api_client.get("/api/me/prefs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    me = resp.json()
    assert me["scope"] == "user"
    assert any(p["kind"] == "pacing" and p["applied"] for p in me["priors"])


async def test_comment_response_reports_learned_prior(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """The comment response confirms what it taught, for teach-time feedback (§8.6)."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_l")
    session_id = await _create_session(api_client, auth_headers, book_id)

    resp = await api_client.post(
        f"/api/sessions/{session_id}/comment",
        headers=auth_headers,
        json={"shot_id": "shot_l", "note": "warmer, and slow it down", "region_png": None},
    )
    assert resp.status_code == 200, resp.text
    learned = {p["kind"] for p in resp.json()["learned"]}
    assert {"pacing", "palette"} <= learned
    # Provenance (§8.6): each learned prior remembers the note that taught it.
    assert all("slow it down" in p["last_note"] for p in resp.json()["learned"])

    # A note with no style cue teaches nothing.
    resp = await api_client.post(
        f"/api/sessions/{session_id}/comment",
        headers=auth_headers,
        json={"shot_id": "shot_l", "note": "this looks great", "region_png": None},
    )
    assert resp.json()["learned"] == []


async def test_reset_clears_book_then_global(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_r")
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _comment(api_client, auth_headers, session_id, "shot_r", "slower, let it linger")
    await _comment(api_client, auth_headers, session_id, "shot_r", "warmer palette please")

    # Per-book reset clears that book's learned style.
    resp = await api_client.delete(f"/api/books/{book_id}/prefs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] >= 1
    resp = await api_client.get(f"/api/books/{book_id}/prefs", headers=auth_headers)
    assert resp.json()["priors"] == []

    # Global reset is idempotent and returns a count (0 now that the book is clear).
    resp = await api_client.delete("/api/me/prefs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "user"


async def test_canon_edit_learns_palette_prior(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    async with container.session_factory() as session:
        assert container.embedder is not None
        canon = CanonService(
            session, embedder=container.embedder, blob_store=container.object_store
        )
        await canon.upsert_entity(
            book_id=book_id,
            entity_key="char_hero",
            entity_type=EntityType.CHARACTER,
            name="Hero",
            valid_from_beat=1,
        )

    edit: dict[str, Any] = {"changes": {"appearance": {"description": "a warmer, golden cloak"}}}
    resp = await api_client.post(
        f"/api/books/{book_id}/canon_edit",
        headers=auth_headers,
        json={"entity_key": "char_hero", **edit},
    )
    assert resp.status_code == 200, resp.text

    resp = await api_client.get(f"/api/books/{book_id}/prefs", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    palette = next(p for p in resp.json()["priors"] if p["kind"] == "palette")
    assert palette["bias"] > 0  # warmer
    assert "Warmer palette bias" in palette["label"]


async def test_book_prefs_require_ownership(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Any],
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    intruder = await make_user("intruder@example.com")
    resp = await api_client.get(f"/api/books/{book_id}/prefs", headers=intruder)
    assert resp.status_code == 404, resp.text
    resp = await api_client.delete(f"/api/books/{book_id}/prefs", headers=intruder)
    assert resp.status_code == 404, resp.text
