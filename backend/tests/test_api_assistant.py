"""Infra-gated API integration tests for the reader-assistant routes (§8 read side).

Drives the real gateway over the throwaway Postgres/Redis/MinIO with the chat
provider faked via the ``container.assistant_chat`` seam (zero credits). Asserts
ownership enforcement, grounded answers, the spoiler horizon at the HTTP layer,
suggestions, and conversation threading. Skips cleanly without infra.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.composition import Container
from app.db.models.enums import EntityType, ShotStatus
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import PageRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo
from tests.assistant_fakes import FakeChat
from tests.conftest import requires_infra, seed_owned_book

pytestmark = requires_infra

Headers = dict[str, str]
MakeUser = Callable[[str], Awaitable[Headers]]


async def _seed(api_client: AsyncClient, container: Container, headers: Headers) -> str:
    book_id = await seed_owned_book(api_client, container, headers)
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=1, scene_id="scene_a"
        )
        await BeatRepo(session).create(
            book_id=book_id,
            scene_id="scene_a",
            beat_index=1,
            summary="Elsa stands at the window.",
            beat_id="beat_1",
            source_span={"page": 1, "word_range": [0, 20]},
        )
        await PageRepo(session).create(
            book_id=book_id, page_number=1, text="Elsa stood alone with a platinum braid."
        )
        ents = EntityRepo(session)
        await ents.upsert_new_version(
            book_id=book_id,
            entity_key="char_elsa",
            entity_type=EntityType.CHARACTER,
            name="Elsa",
            valid_from_beat=1,
            description="A young woman with a platinum braid and an ice-blue gown.",
        )
        # Future spoiler entity at beat 9.
        await ents.upsert_new_version(
            book_id=book_id,
            entity_key="char_duke",
            entity_type=EntityType.CHARACTER,
            name="Duke",
            valid_from_beat=9,
            description="The Duke betrays the queen near the end.",
        )
        await ShotRepo(session).create(
            id="shot_1",
            book_id=book_id,
            scene_id="scene_a",
            beat_id="beat_1",
            status=ShotStatus.ACCEPTED,
            duration_s=5.0,
            narration={"text": "Elsa at the frosted window."},
        )
    return book_id


async def test_ask_returns_grounded_answer(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    container.assistant_chat = FakeChat(
        answer="Elsa has a platinum braid and an ice-blue gown [1].", citations=[1]
    )
    book_id = await _seed(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/ask",
        headers=auth_headers,
        json={"question": "Who is Elsa?", "beat_index": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "who_is"
    assert body["grounded"] is True
    assert body["citations"]
    assert body["suggestions"]


async def test_ask_unauthenticated_rejected(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    book_id = await _seed(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/ask", json={"question": "Who is Elsa?"}
    )
    assert resp.status_code == 401


async def test_ask_other_users_book_is_404(
    api_client: AsyncClient,
    container: Container,
    auth_headers: Headers,
    make_user: MakeUser,
) -> None:
    container.assistant_chat = FakeChat()
    book_id = await _seed(api_client, container, auth_headers)
    other = await make_user("intruder@example.com")
    resp = await api_client.post(
        f"/api/books/{book_id}/ask",
        headers=other,
        json={"question": "Who is Elsa?"},
    )
    assert resp.status_code == 404


async def test_ask_spoiler_horizon_blocks_future_entity(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    # Even if the model tried to cite the future Duke, that span is never in
    # context for a reader at beat 1, so it cannot be a valid citation.
    container.assistant_chat = FakeChat(answer="Elsa has a braid [1].", citations=[1])
    book_id = await _seed(api_client, container, auth_headers)
    resp = await api_client.post(
        f"/api/books/{book_id}/ask",
        headers=auth_headers,
        json={"question": "Who is the Duke?", "beat_index": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "canon:char_duke" not in body["context_span_ids"]


async def test_suggestions_endpoint(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    container.assistant_chat = FakeChat()
    book_id = await _seed(api_client, container, auth_headers)
    resp = await api_client.get(
        f"/api/books/{book_id}/suggestions?beat_index=5", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    suggestions = resp.json()["suggestions"]
    assert suggestions
    # No suggestion may point at the future Duke entity.
    assert all(s.get("about_entity_key") != "char_duke" for s in suggestions)


async def test_conversation_threading_and_clear(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    container.assistant_chat = FakeChat(answer="Elsa has a braid [1].", citations=[1])
    book_id = await _seed(api_client, container, auth_headers)
    conv = "conv-test-1"
    await api_client.post(
        f"/api/books/{book_id}/ask",
        headers=auth_headers,
        json={"question": "Who is Elsa?", "beat_index": 5, "conversation_id": conv},
    )
    got = await api_client.get(
        f"/api/books/{book_id}/conversations/{conv}", headers=auth_headers
    )
    assert got.status_code == 200, got.text
    turns = got.json()["turns"]
    assert len(turns) == 2
    assert turns[0]["role"] == "user"

    cleared = await api_client.delete(
        f"/api/books/{book_id}/conversations/{conv}", headers=auth_headers
    )
    assert cleared.status_code == 204
    after = await api_client.get(
        f"/api/books/{book_id}/conversations/{conv}", headers=auth_headers
    )
    assert after.json()["turns"] == []


async def test_ask_stream_emits_sse_done(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    container.assistant_chat = FakeChat(answer="Elsa has a braid [1].", citations=[1])
    book_id = await _seed(api_client, container, auth_headers)
    async with api_client.stream(
        "POST",
        f"/api/books/{book_id}/ask/stream",
        headers=auth_headers,
        json={"question": "Who is Elsa?", "beat_index": 5},
    ) as resp:
        assert resp.status_code == 200
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
    assert "event: done" in body
    # The terminal frame carries the grounded answer payload.
    done_frames = [ln for ln in body.splitlines() if ln.startswith("data:") and "answer" in ln]
    assert done_frames
    payload = json.loads(done_frames[-1][len("data:") :])
    assert payload["answer"]["grounded"] is True
