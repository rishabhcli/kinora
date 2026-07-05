"""Director endpoint tests — comment routing + surgical canon-edit regen (§5.4/§8.7)."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from redis.asyncio.client import PubSub

from app.composition import Container
from app.db.models.enums import EntityType, RenderPriority, ShotStatus
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from app.memory.conflict_log import record_conflict_history
from app.memory.interfaces import ShotSpec
from app.queue.redis_queue import book_channel, conflict_object_key, session_channel
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
        assert body["affected_shot_ids"] == ["shot_dep"]
        assert body["skipped_shots"] == 1

        # Only the dependent shot was enqueued (surgical re-render, §8.7).
        assert [c[0] for c in recorder.calls] == ["shot_dep"]
        assert recorder.calls[0][1] == RenderPriority.COMMITTED

        # regen_done is announced as the dependent shot completes (after the
        # Continuity Supervisor's canon-edit announcement on the same channel).
        events = await _collect(container, pubsub, until="regen_done")
    regen = next((e for e in events if e.get("event") == "regen_done"), None)
    assert regen is not None, events
    assert regen["shot_id"] == "shot_dep"
    assert regen["oss_url"]


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


async def _collect(
    container: Container, pubsub: PubSub, *, until: str, limit: int = 6
) -> list[dict]:
    """Read up to ``limit`` session events, stopping once ``until`` is seen."""
    events: list[dict] = []
    for _ in range(limit):
        msg = await container.redis.next_message(pubsub, timeout=5.0)
        if msg is None:
            break
        events.append(msg)
        if msg.get("event") == until:
            break
    return events


async def _drain_bg(container: Container) -> None:
    """Await the background task the conflict_choice handler spawned (it regenerates
    / evolves canon), so it can't bleed into a later test under random ordering."""
    pending = [t for t in container._bg_tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=5.0)


async def test_conflict_choice_honor_regenerates_disputed_shot(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """honor_canon streams the Showrunner's arbitration, then regenerates the
    disputed shot — the §16 lost-sword conflict resolving live."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_51", [])
    # Surface the conflict the way the render worker does (so the handler can act).
    await container.redis.set_json(
        conflict_object_key(session_id, "cf_shot_51"),
        {
            "conflict_id": "cf_shot_51",
            "shot_id": "shot_51",
            "claim": "the heroine draws a sword she lost",
            "canon_fact": "state_hero_sword_001 retired at beat_0034 (sword lost in the river)",
            "current_beat": "beat_0039",
            "options": [],
        },
    )
    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/conflict_choice",
            headers=auth_headers,
            json={"conflict_id": "cf_shot_51", "option": "honor_canon"},
        )
        assert resp.status_code == 200, resp.text
        events = await _collect(container, pubsub, until="regen_done")
    await _drain_bg(container)

    arb = events[0]
    assert arb["event"] == "agent_activity"
    assert arb["agent"] == "showrunner"
    assert arb["conflict"]["option"] == "honor_canon"
    assert "honour canon" in arb["message"]

    regen = next((e for e in events if e.get("event") == "regen_done"), None)
    assert regen is not None, events
    assert regen["shot_id"] == "shot_51"


async def test_conflict_choice_evolve_writes_canon_and_regenerates(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """evolve_canon asserts a versioned canon fact (§8.5), then regenerates the shot."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_77", [])
    await container.redis.set_json(
        conflict_object_key(session_id, "cf_shot_77"),
        {
            "conflict_id": "cf_shot_77",
            "shot_id": "shot_77",
            "claim": "the hero wields the lost sword again",
            "canon_fact": "state_hero_sword_001 retired at beat_0034",
            "current_beat": None,
            "contradicting_state_id": None,
            "options": [],
        },
    )
    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/conflict_choice",
            headers=auth_headers,
            json={"conflict_id": "cf_shot_77", "option": "evolve_canon"},
        )
        assert resp.status_code == 200, resp.text
        events = await _collect(container, pubsub, until="regen_done")
    await _drain_bg(container)

    assert any(
        e.get("event") == "agent_activity" and "Canon evolved" in e.get("message", "")
        for e in events
    ), events
    assert any(
        e.get("event") == "regen_done" and e.get("shot_id") == "shot_77" for e in events
    ), events

    # Canon genuinely gained a versioned fact from the evolve (§8.5).
    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        states = await canon.active_states_at_beat(book_id, 0)
    assert any(s.predicate == "canon_evolved" for s in states), states


async def test_conflict_choice_evolve_does_not_reassert_the_contradicted_states_old_value(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """Regression (independent review, 2026-07-05 — the same bug found and
    fixed in ConflictResolver._evolve_canon, duplicated here in the
    director-facing evolve path): when contradicting_state_id DOES match a
    real, currently-active canon fact, the write must NOT reuse that fact's
    own (subject, predicate, object_value) — that's the OLD value being
    contradicted, not the new one the director is establishing."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_78", [])

    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        old_state_id = await canon.assert_state(
            book_id=book_id,
            subject_entity_key="char_hero",
            predicate="located_in",
            object_value="loc_forest",
            valid_from_beat=0,
        )

    await container.redis.set_json(
        conflict_object_key(session_id, "cf_shot_78"),
        {
            "conflict_id": "cf_shot_78",
            "shot_id": "shot_78",
            "claim": "the hero now stands in the castle courtyard",
            "canon_fact": "char_hero located_in loc_forest",
            "current_beat": None,
            "contradicting_state_id": old_state_id,
            "options": [],
        },
    )
    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/conflict_choice",
            headers=auth_headers,
            json={"conflict_id": "cf_shot_78", "option": "evolve_canon"},
        )
        assert resp.status_code == 200, resp.text
        await _collect(container, pubsub, until="regen_done")
    await _drain_bg(container)

    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        states = await canon.active_states_at_beat(book_id, 0)
    evolved = [s for s in states if s.predicate == "canon_evolved"]
    assert len(evolved) == 1, states
    # Never the old, contradicted fact's own fields.
    assert (evolved[0].subject_entity_key, evolved[0].object_value) != (
        "char_hero",
        "loc_forest",
    )
    assert evolved[0].object_value == "the hero now stands in the castle courtyard"


async def test_conflict_choice_is_idempotent(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """A repeat pick for an already-applied conflict is a no-op (no second regen)."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_idem", [])
    await container.redis.set_json(
        conflict_object_key(session_id, "cf_shot_idem"),
        {"conflict_id": "cf_shot_idem", "shot_id": "shot_idem", "claim": "x", "options": []},
    )

    first = await api_client.post(
        f"/api/sessions/{session_id}/conflict_choice",
        headers=auth_headers,
        json={"conflict_id": "cf_shot_idem", "option": "honor_canon"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "applied"

    second = await api_client.post(
        f"/api/sessions/{session_id}/conflict_choice",
        headers=auth_headers,
        json={"conflict_id": "cf_shot_idem", "option": "honor_canon"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "already_resolved"
    await _drain_bg(container)


async def test_conflict_choice_rejects_unknown_option(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """An option outside the fixed §7.2 policy set is a 422 (typed enum)."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    resp = await api_client.post(
        f"/api/sessions/{session_id}/conflict_choice",
        headers=auth_headers,
        json={"conflict_id": "cf_x", "option": "delete_everything"},
    )
    assert resp.status_code == 422, resp.text


async def test_list_conflicts_returns_resolved_history(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """The §7.2 history endpoint replays a surfaced+resolved conflict for a refresh."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_hist", [])
    obj = {
        "conflict_id": "cf_shot_hist",
        "shot_id": "shot_hist",
        "claim": "the heroine draws a sword she lost",
        "canon_fact": "state_hero_sword_001 retired at beat_0034",
        "raised_by": "continuity_supervisor",
        "options": [{"id": "honor_canon", "action": "regenerate empty-handed"}],
    }
    await container.redis.set_json(conflict_object_key(session_id, "cf_shot_hist"), obj)
    await record_conflict_history(
        container.redis, session_id, conflict=obj, conflict_id="cf_shot_hist"
    )

    resp = await api_client.post(
        f"/api/sessions/{session_id}/conflict_choice",
        headers=auth_headers,
        json={"conflict_id": "cf_shot_hist", "option": "honor_canon"},
    )
    assert resp.status_code == 200, resp.text
    await _drain_bg(container)

    hist = await api_client.get(
        f"/api/sessions/{session_id}/conflicts", headers=auth_headers
    )
    assert hist.status_code == 200, hist.text
    rec = next((r for r in hist.json() if r["conflict_id"] == "cf_shot_hist"), None)
    assert rec is not None, hist.json()
    assert rec["resolved"] is True
    assert rec["chosen_option"] == "honor_canon"
    assert rec["shot_id"] == "shot_hist"
    assert "sword" in (rec["claim"] or "")


async def test_demo_conflict_surfaces_lost_sword(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """The dev-only demo trigger surfaces the §7.2 lost-sword conflict live (§16)."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    await _seed_shot(container, book_id, "shot_demo", [])

    async with container.redis.subscribe(session_channel(session_id)) as pubsub:
        resp = await api_client.post(
            f"/api/sessions/{session_id}/demo/conflict", headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()
        assert rec["shot_id"] == "shot_demo"
        assert "sword" in (rec["claim"] or "")
        assert len(rec["options"]) == 3
        events = await _collect(container, pubsub, until="conflict_choice")

    cc = next((e for e in events if e.get("event") == "conflict_choice"), None)
    assert cc is not None, events
    assert cc["conflict_id"] == "cf_shot_demo"

    hist = await api_client.get(
        f"/api/sessions/{session_id}/conflicts", headers=auth_headers
    )
    assert any(r["conflict_id"] == "cf_shot_demo" for r in hist.json())
