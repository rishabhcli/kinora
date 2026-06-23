"""Event-channel tests — SSE fan-out + WebSocket round-trips (§5.6)."""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import AsyncGenerator
from typing import cast

from fastapi import FastAPI
from httpx import AsyncClient
from starlette.requests import Request
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.routes.events import library_events, session_events
from app.composition import Container, build_container
from app.main import create_app
from app.queue.redis_queue import library_channel, session_channel
from tests import conftest as cf
from tests.conftest import (
    FakeCommentClassifier,
    FakeEmbedder,
    FakeIngestRunner,
    build_test_settings,
    requires_infra,
    seed_owned_book,
    tiny_pdf,
)


async def _create_session(client: AsyncClient, headers: dict[str, str], book_id: str) -> str:
    resp = await client.post("/api/sessions", headers=headers, json={"book_id": book_id})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["session_id"])


def _as_text(chunk: object) -> str:
    return chunk.decode() if isinstance(chunk, bytes) else str(chunk)


async def test_sse_forwards_published_event(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """The SSE generator subscribes and forwards a §5.6 event to the client.

    httpx's ``ASGITransport`` buffers the whole response body (it cannot consume
    an infinite SSE stream), so we drive the real endpoint's StreamingResponse
    body iterator directly — exercising token auth, ownership, the Redis
    subscription, and the publish→forward path against the real channel.
    """
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    token = auth_headers["Authorization"].split(" ", 1)[1]

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(container=container))

    async def receive() -> dict[str, object]:
        # Block; Request.is_disconnected() probes with a cancelled scope, so this
        # reads as "still connected" without ever signalling a disconnect.
        await asyncio.get_event_loop().create_future()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/sessions/{session_id}/events",
        "headers": [],
        "query_string": f"token={token}".encode(),
        "client": ("test", 1),
        "server": ("test", 80),
        "scheme": "http",
        "app": fake_app,
    }
    request = Request(scope, receive)
    response = await session_events(session_id, request, token=token)
    body = cast("AsyncGenerator[bytes | str, None]", response.body_iterator)

    connected = await asyncio.wait_for(body.__anext__(), timeout=5.0)
    assert "connected" in _as_text(connected)

    await container.redis.publish(
        session_channel(session_id),
        {"event": "clip_ready", "shot_id": "shot_x", "oss_url": "https://x/clip"},
    )

    found = ""
    try:
        for _ in range(10):
            chunk = _as_text(await asyncio.wait_for(body.__anext__(), timeout=6.0))
            if "event: clip_ready" in chunk:
                found = chunk
                break
    finally:
        await body.aclose()

    assert "event: clip_ready" in found
    payload = json.loads(found.split("data:", 1)[1].strip())
    assert payload["shot_id"] == "shot_x"
    assert payload["oss_url"] == "https://x/clip"


async def test_library_sse_forwards_ingest_progress(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """The shelf SSE stream forwards live ingest_progress on the library channel."""
    from app.db.repositories.user import UserRepo

    token = auth_headers["Authorization"].split(" ", 1)[1]
    async with container.session_factory() as session:
        user = await UserRepo(session).get_by_email("owner@example.com")
    assert user is not None

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(container=container))

    async def receive() -> dict[str, object]:
        await asyncio.get_event_loop().create_future()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/books/events",
        "headers": [],
        "query_string": f"token={token}".encode(),
        "client": ("test", 1),
        "server": ("test", 80),
        "scheme": "http",
        "app": fake_app,
    }
    request = Request(scope, receive)
    response = await library_events(request, token=token)
    body = cast("AsyncGenerator[bytes | str, None]", response.body_iterator)

    connected = await asyncio.wait_for(body.__anext__(), timeout=5.0)
    assert "connected" in _as_text(connected)

    await container.redis.publish(
        library_channel(user.id),
        {"event": "ingest_progress", "book_id": "book_x", "stage": "analyse", "pct": 0.42},
    )

    found = ""
    try:
        for _ in range(10):
            chunk = _as_text(await asyncio.wait_for(body.__anext__(), timeout=6.0))
            if "event: ingest_progress" in chunk:
                found = chunk
                break
    finally:
        await body.aclose()

    assert "event: ingest_progress" in found
    payload = json.loads(found.split("data:", 1)[1].strip())
    assert payload["book_id"] == "book_x"
    assert payload["stage"] == "analyse"
    assert payload["pct"] == 0.42


def _ws_app() -> FastAPI:
    container = build_container(build_test_settings())
    container.embedder = FakeEmbedder()
    container.comment_classifier = FakeCommentClassifier()
    container.ingest_runner = FakeIngestRunner(container)
    app = create_app()
    app.state.container = container
    app.state.run_idle_sweeper = False
    return app


@requires_infra
def test_ws_rejects_unauthorized() -> None:
    app = _ws_app()
    with TestClient(app) as tc:
        try:
            with tc.websocket_connect("/api/ws/sessions/x"):
                pass
        except WebSocketDisconnect:
            return
        raise AssertionError("expected WebSocketDisconnect for an unauthorized WS")


@requires_infra
def test_ws_bidirectional_roundtrip() -> None:
    import redis as redis_sync

    app = _ws_app()
    with TestClient(app) as tc:
        tc.post("/api/auth/register", json={"email": "ws@example.com", "password": "password123"})
        tok = tc.post(
            "/api/auth/login", json={"email": "ws@example.com", "password": "password123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}
        up = tc.post(
            "/api/books", headers=headers, files={"file": ("t.pdf", tiny_pdf(), "application/pdf")}
        )
        book_id = up.json()["id"]
        session_id = tc.post(
            "/api/sessions", headers=headers, json={"book_id": book_id}
        ).json()["session_id"]

        with tc.websocket_connect(f"/api/ws/sessions/{session_id}?token={tok}") as ws:
            # client -> backend: an intent update is accepted without error.
            ws.send_json({"type": "intent_update", "focus_word": 5, "velocity": 4.0})
            # backend -> client: an event published to the channel is fanned out.
            redis_url = cf._REDIS_URL
            assert redis_url is not None
            pub = redis_sync.Redis.from_url(redis_url, decode_responses=True)
            try:
                pub.publish(
                    session_channel(session_id),
                    json.dumps({"event": "agent_activity", "agent": "showrunner", "message": "hi"}),
                )
                data = ws.receive_json()
            finally:
                pub.close()
        assert data["event"] == "agent_activity"
        assert data["agent"] == "showrunner"
