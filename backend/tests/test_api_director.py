"""Director endpoint tests — comment routing + surgical canon-edit regen (§5.4/§8.7)."""

from __future__ import annotations

from httpx import AsyncClient

from app.composition import Container
from app.db.models.enums import EntityType, RenderPriority, ShotStatus
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from app.memory.interfaces import ShotSpec
from app.queue.redis_queue import book_channel, session_channel
from tests.conftest import seed_owned_book


class RecordingEnqueuer:
    """A test double for the injected RenderEnqueuer that records enqueue calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, RenderPriority]] = []

    async def enqueue(
        self, shot_spec: ShotSpec, priority: RenderPriority, cancel_token: str | None = None
    ) -> str:
        self.calls.append((shot_spec.shot_id or "", priority))
        return f"job_{shot_spec.shot_id}"


async def _create_session(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    resp = await client.post("/api/sessions", headers=headers, json={"book_id": book_id})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_id"])


async def _seed_shot(container: Container, book_id: str, shot_id: str, refs: list[str]) -> None:
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
            reference_image_ids=refs,
            canon_version_at_render=1,
        )


async def test_comment_routes_to_agent_and_emits_activity(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_c", ["char_hero@v1"])
    session_id = await _create_session(api_client, auth_headers, book_id)

    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/comment",
            headers=auth_headers,
            json={"shot_id": "shot_c", "note": "this shot is too fast", "region_png": None},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent"] == "cinematographer"
        assert body["aspect"] == "pacing"
        assert body["job_id"]

        event = await container.redis.next_message(pubsub, timeout=5.0)
    assert event is not None
    assert event["event"] == "agent_activity"
    assert event["agent"] == "cinematographer"
    assert event["shot_id"] == "shot_c"


async def test_comment_routes_continuity_for_room(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_r", ["loc_hall@v1"])
    session_id = await _create_session(api_client, auth_headers, book_id)
    resp = await api_client.post(
        f"/api/sessions/{session_id}/comment",
        headers=auth_headers,
        json={"shot_id": "shot_r", "note": "this is the wrong room entirely"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent"] == "continuity"


async def test_canon_edit_surgical_regen_only_dependent(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    # Two shots: one depends on char_hero, one does not.
    await _seed_shot(container, book_id, "shot_dep", ["char_hero@v1", "loc_hall@v1"])
    await _seed_shot(container, book_id, "shot_indep", ["char_villain@v1"])
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

    # Capture which shots get enqueued for regen (the injected DI seam).
    recorder = RecordingEnqueuer()
    container.render_enqueuer = recorder

    async with container.redis.subscribe(book_channel(book_id)) as pubsub:
        resp = await api_client.post(
            f"/api/books/{book_id}/canon_edit",
            headers=auth_headers,
            json={"entity_key": "char_hero", "changes": {"description": "now wears a red coat"}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == 2
        assert body["regenerated_shots"] == ["shot_dep"]
        assert body["skipped_shots"] == 1

        # Only the dependent shot was enqueued (surgical re-render, §8.7).
        assert [c[0] for c in recorder.calls] == ["shot_dep"]
        assert recorder.calls[0][1] == RenderPriority.COMMITTED

        # regen_done is announced as the dependent shot completes.
        event = await container.redis.next_message(pubsub, timeout=5.0)
    assert event is not None
    assert event["event"] == "regen_done"
    assert event["shot_id"] == "shot_dep"
    assert event["oss_url"]


async def test_canon_edit_recomputes_dependent_shot_hash(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_shot(container, book_id, "shot_dep", ["char_hero@v1"])
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
    container.render_enqueuer = RecordingEnqueuer()

    resp = await api_client.post(
        f"/api/books/{book_id}/canon_edit",
        headers=auth_headers,
        json={"entity_key": "char_hero", "changes": {"description": "taller"}},
    )
    assert resp.status_code == 200

    async with container.session_factory() as session:
        shot = await ShotRepo(session).get("shot_dep")
    assert shot is not None
    # The edit bumped canon to v2 and the dependent shot's hash was recomputed.
    assert shot.canon_version_at_render == 2
    assert shot.shot_hash is not None
    assert shot.status is ShotStatus.PLANNED


async def test_conflict_choice_records_and_announces(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/conflict_choice",
            headers=auth_headers,
            json={"conflict_id": "cf_1", "option": "honor_canon"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "recorded"
        event = await container.redis.next_message(pubsub, timeout=5.0)
    assert event is not None
    assert event["event"] == "agent_activity"
    assert event["conflict"]["conflict_id"] == "cf_1"
