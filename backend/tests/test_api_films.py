"""Integration tests for the film routes (skip without throwaway infra).

The tests mount the router explicitly on a fresh app so the route behavior stays
isolated from unrelated application wiring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.composition import Container
from app.db.models.enums import SessionMode, ShotStatus
from app.db.repositories.beat import BeatRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.session import SessionRepo
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from app.main import create_app
from app.storage.object_store import keys
from tests.conftest import register_login, seed_owned_book


@pytest_asyncio.fixture
async def films_client(container: Container) -> AsyncIterator[AsyncClient]:
    """A client over an app with the films router mounted."""
    from app.api.routes import films

    app = create_app()
    app.state.container = container
    app.state.run_idle_sweeper = False
    app.include_router(films.router, prefix="/api")
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def _segment(shot_id: str, page: int, word_start: int) -> dict[str, Any]:
    """A per-shot, 0-based §9.4 sync segment as stored in shot.narration."""
    return {
        "shot_id": shot_id,
        "video_start_s": 0.0,
        "video_end_s": 5.0,
        "page": page,
        "page_turn_at_s": 4.8,
        "words": [
            {"word_index": word_start, "text": "She", "t_start": 0.1, "t_end": 0.9, "bbox": None},
        ],
    }


async def _seed_scene(container: Container, book_id: str) -> None:
    """One scene with two accepted shots (each with a per-shot sync segment + span)."""
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=2, scene_id="scene_0"
        )
        shots = ShotRepo(session)
        spans = SourceSpanRepo(session)
        for i, (sid, start, page) in enumerate([("shot_0", 100, 1), ("shot_1", 150, 2)]):
            await shots.create(
                id=sid,
                book_id=book_id,
                scene_id="scene_0",
                beat_id=f"beat_{i}",
                status=ShotStatus.ACCEPTED,
                duration_s=5.0,
                source_span={"page": page, "word_range": [start, start + 40]},
                narration={"sync_segment": _segment(sid, page, start)},
                output={"clip_key": keys.clip(book_id, sid)},
            )
            await spans.bulk_insert(
                [
                    {
                        "book_id": book_id,
                        "word_index_start": start,
                        "word_index_end": start + 40,
                        "shot_id": sid,
                        "scene_id": "scene_0",
                        "beat_id": f"beat_{i}",
                    }
                ]
            )


async def test_list_events_builds_cumulative_sync_map(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    await _seed_scene(container, book_id)

    resp = await films_client.get(f"/api/books/{book_id}/events", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["book_id"] == book_id
    assert body["url_ttl_s"] > 0
    assert body["restore"] is None  # no session yet
    assert len(body["events"]) == 1

    event = body["events"][0]
    assert event["event_id"] == "scene_0"
    assert event["event_index"] == 0
    assert event["shot_count"] == 2
    assert event["word_range"] == [100, 190]  # min start .. max end across shots
    # event == scene today: scenes[] holds the one composing scene.
    assert [s["scene_id"] for s in event["scenes"]] == ["scene_0"]

    segs = event["sync_map"]["segments"]
    assert [s["shot_id"] for s in segs] == ["shot_0", "shot_1"]
    # shot_1 is shifted onto the film timeline by shot_0's 5s duration.
    assert (segs[0]["t_start_s"], segs[0]["t_end_s"]) == (0.0, 5.0)
    assert (segs[1]["t_start_s"], segs[1]["t_end_s"]) == (5.0, 10.0)
    assert segs[1]["page_turn_at_s"] == 9.8
    assert segs[1]["scene_id"] == "scene_0"
    assert segs[1]["word_range"] == [150, 190]
    assert event["sync_map"]["duration_s"] == 10.0


async def test_unstitched_event_has_null_url(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films2@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    await _seed_scene(container, book_id)  # no stitched mp4 put in the store

    body = (await films_client.get(f"/api/books/{book_id}/events", headers=headers)).json()
    event = body["events"][0]
    assert event["stitched"] is False
    assert event["oss_url"] is None
    assert event["url_expires_at"] is None


async def test_stitched_event_presigns_url(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films3@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    await _seed_scene(container, book_id)
    # Simulate the render pipeline having stitched the scene mp4 into object storage.
    clip_key = keys.clip(book_id, "scene_0")
    await anyio.to_thread.run_sync(
        container.object_store.put_bytes, clip_key, b"\x00\x00\x00", "video/mp4"
    )

    body = (await films_client.get(f"/api/books/{book_id}/events", headers=headers)).json()
    event = body["events"][0]
    assert event["stitched"] is True
    assert event["oss_url"]
    assert "scene_0.mp4" in event["oss_url"]


async def test_get_scene_film_partial_load(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films4@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    await _seed_scene(container, book_id)

    resp = await films_client.get(
        f"/api/books/{book_id}/scenes/scene_0/film", headers=headers
    )
    assert resp.status_code == 200, resp.text
    film = resp.json()
    assert film["scene_id"] == "scene_0"
    assert film["event_id"] == "scene_0"
    assert film["shot_count"] == 2
    assert len(film["sync_map"]["segments"]) == 2


async def test_scene_film_404_for_unknown_scene(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films5@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    resp = await films_client.get(
        f"/api/books/{book_id}/scenes/nope/film", headers=headers
    )
    assert resp.status_code == 404


async def test_events_404_for_foreign_book(
    films_client: AsyncClient, container: Container
) -> None:
    owner = await register_login(films_client, "owner-a@example.com")
    book_id = await seed_owned_book(films_client, container, owner)
    intruder = await register_login(films_client, "intruder@example.com")
    resp = await films_client.get(f"/api/books/{book_id}/events", headers=intruder)
    assert resp.status_code == 404


async def test_restore_state_from_latest_session(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films6@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    await _seed_scene(container, book_id)
    # Find the user id, then drop a prior session whose focus word sits in shot_1's span.
    me = (await films_client.get("/api/auth/me", headers=headers)).json()
    async with container.session_factory() as session:
        await SessionRepo(session).upsert(
            session_id="sess_restore",
            book_id=book_id,
            user_id=me["id"],
            focus_word=160,
            mode=SessionMode.VIEWER,
            last_activity_ms=999,
        )

    body = (await films_client.get(f"/api/books/{book_id}/events", headers=headers)).json()
    restore = body["restore"]
    assert restore is not None
    assert restore["session_id"] == "sess_restore"
    assert restore["focus_word"] == 160
    assert restore["current_event_index"] == 0
    assert restore["current_scene_id"] == "scene_0"
    assert restore["mode"] == "viewer"


async def test_event_with_no_accepted_shots_is_empty(
    films_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(films_client, "films7@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=1, scene_id="scene_empty"
        )

    body = (await films_client.get(f"/api/books/{book_id}/events", headers=headers)).json()
    event = body["events"][0]
    assert event["shot_count"] == 0
    assert event["stitched"] is False
    assert event["duration_s"] is None
    assert event["sync_map"]["segments"] == []
    assert event["word_range"] == [0, 0]


async def test_shot_without_sync_segment_is_synthesized(
    films_client: AsyncClient, container: Container
) -> None:
    """A shot lacking narration.sync_segment still yields a (word-less) segment."""
    headers = await register_login(films_client, "films8@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=1, scene_id="scene_s"
        )
        await ShotRepo(session).create(
            id="shot_bare",
            book_id=book_id,
            scene_id="scene_s",
            beat_id="beat_0",
            status=ShotStatus.ACCEPTED,
            duration_s=4.0,
            source_span={"page": 1, "word_range": [10, 20]},
            narration=None,
        )

    film = (
        await films_client.get(f"/api/books/{book_id}/scenes/scene_s/film", headers=headers)
    ).json()
    assert film["shot_count"] == 1
    seg = film["sync_map"]["segments"][0]
    assert seg["shot_id"] == "shot_bare"
    assert (seg["t_start_s"], seg["t_end_s"]) == (0.0, 4.0)
    assert seg["page"] == 1
    assert seg["page_turn_at_s"] == 3.8
    assert seg["word_range"] == [10, 20]
    assert seg["words"] == []


async def test_events_order_shots_by_beat_index_not_word_span(
    films_client: AsyncClient, container: Container
) -> None:
    """Shot order follows Beat.beat_index (matching the stitcher), not word-span start."""
    headers = await register_login(films_client, "films9@example.com")
    book_id = await seed_owned_book(films_client, container, headers)
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=1, scene_id="scene_b"
        )
        beats = BeatRepo(session)
        shots = ShotRepo(session)
        # Adversarial: the shot with the LATER word span has the EARLIER beat_index.
        # The stitcher concatenates by beat_index, so the film API must too.
        await beats.create(
            book_id=book_id, scene_id="scene_b", beat_index=0, summary="b0", beat_id="beat_first"
        )
        await beats.create(
            book_id=book_id, scene_id="scene_b", beat_index=1, summary="b1", beat_id="beat_second"
        )
        await shots.create(
            id="shot_late_word",
            book_id=book_id,
            scene_id="scene_b",
            beat_id="beat_first",
            status=ShotStatus.ACCEPTED,
            duration_s=5.0,
            source_span={"page": 1, "word_range": [900, 950]},
            narration={"sync_segment": _segment("shot_late_word", 1, 900)},
        )
        await shots.create(
            id="shot_early_word",
            book_id=book_id,
            scene_id="scene_b",
            beat_id="beat_second",
            status=ShotStatus.ACCEPTED,
            duration_s=5.0,
            source_span={"page": 1, "word_range": [100, 150]},
            narration={"sync_segment": _segment("shot_early_word", 1, 100)},
        )

    body = (await films_client.get(f"/api/books/{book_id}/events", headers=headers)).json()
    segs = body["events"][0]["sync_map"]["segments"]
    # beat_index 0 (shot_late_word) comes first despite its larger word span.
    assert [s["shot_id"] for s in segs] == ["shot_late_word", "shot_early_word"]
    assert (segs[0]["t_start_s"], segs[1]["t_start_s"]) == (0.0, 5.0)
