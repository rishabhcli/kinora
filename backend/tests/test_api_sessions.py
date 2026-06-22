"""Session endpoint tests — create + intent (drives the Scheduler) + seek (§4)."""

from __future__ import annotations

from httpx import AsyncClient

from app.composition import Container
from app.db.models.enums import RenderPriority, ShotStatus
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from tests.conftest import register_login, seed_owned_book


async def _seed_shots_and_spans(container: Container, book_id: str) -> None:
    starts = [40, 100, 160]
    async with container.session_factory() as session:
        shots = ShotRepo(session)
        for i, start in enumerate(starts):
            await shots.create(
                id=f"shot_{i}",
                book_id=book_id,
                beat_id=f"beat_{i}",
                scene_id="scene_1",
                status=ShotStatus.PLANNED,
                duration_s=5.0,
                source_span={"word_range": [start, start + 50]},
            )
        await SourceSpanRepo(session).bulk_insert(
            [
                {
                    "book_id": book_id,
                    "word_index_start": start,
                    "word_index_end": start + 50,
                    "shot_id": f"shot_{i}",
                    "beat_id": f"beat_{i}",
                    "scene_id": "scene_1",
                }
                for i, start in enumerate(starts)
            ]
        )


async def _create_session(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    resp = await client.post("/api/sessions", headers=headers, json={"book_id": book_id})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_id"])


async def test_create_session(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    resp = await api_client.post(
        "/api/sessions", headers=auth_headers, json={"book_id": book_id, "mode": "viewer"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["book_id"] == book_id
    assert body["mode"] == "viewer"
    assert body["session_id"].startswith("sess_")


async def test_create_session_unknown_book_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/sessions", headers=auth_headers, json={"book_id": "nope"}
    )
    assert resp.status_code == 404


async def test_intent_drives_scheduler_keyframe_lane(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shots_and_spans(container, book_id)
    session_id = await _create_session(api_client, auth_headers, book_id)

    resp = await api_client.post(
        f"/api/sessions/{session_id}/intent",
        headers=auth_headers,
        json={"focus_word": 0, "velocity": 4.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settled"] is True
    # on_event ran the §4.4 keyframe lane: a still was enqueued per upcoming beat.
    assert len(body["keyframed"]) == 3
    assert await container.queue.depth(RenderPriority.KEYFRAME) == 3


async def test_seek_returns_bridge(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shots_and_spans(container, book_id)
    session_id = await _create_session(api_client, auth_headers, book_id)

    resp = await api_client.post(
        f"/api/sessions/{session_id}/seek", headers=auth_headers, json={"word": 120}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["word"] == 120
    # word 120 resolves to the shot starting at 100 -> its beat bridges the seek.
    assert body["bridge_beat"] == "beat_1"


async def test_get_session_state(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    resp = await api_client.get(f"/api/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == session_id
    assert resp.json()["book_id"] == book_id


async def test_session_not_owned_is_404(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    other = await register_login(api_client, "stranger@example.com")
    resp = await api_client.get(f"/api/sessions/{session_id}", headers=other)
    assert resp.status_code == 404
