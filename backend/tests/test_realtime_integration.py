"""Integration tests for the realtime layer (throwaway Redis/Postgres, §5.6).

Exercises the Redis-backed pieces against the isolated stack the API fixtures
already provision: the event log + resume, presence join/move/leave + fan-out,
the connection registry caps + reaping, idempotency replay, and the HTTP routes
(presence, stream/info, paginated history, versions). Skips cleanly with no infra.
"""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.requests import Request
from starlette.testclient import TestClient

from app.api.realtime.connections import ConnectionLimitExceeded, ConnectionRegistry
from app.api.realtime.event_log import EventLog
from app.api.realtime.idempotency import IdempotencyStore, fingerprint
from app.api.realtime.presence import PresenceService
from app.api.realtime.recorder import EventRecorder, session_id_from_channel
from app.api.realtime.routes_realtime import session_stream
from app.composition import Container, build_container
from app.main import create_app
from app.queue.redis_queue import session_channel
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


# --------------------------------------------------------------------------- #
# Event log + resume
# --------------------------------------------------------------------------- #


async def test_event_log_append_assigns_monotonic_ids(container: Container) -> None:
    log = EventLog(container.redis, namespace="kinora:test:evlog1")
    a = await log.append("sessA", {"event": "clip_ready", "shot_id": "s1"})
    b = await log.append("sessA", {"event": "clip_ready", "shot_id": "s2"})
    assert a == 1 and b == 2
    assert await log.latest_id("sessA") == 2
    assert await log.oldest_id("sessA") == 1
    assert await log.size("sessA") == 2


async def test_event_log_replay_after_returns_only_newer(container: Container) -> None:
    log = EventLog(container.redis, namespace="kinora:test:evlog2")
    for i in range(5):
        await log.append("sessB", {"event": "tick", "n": i})
    result = await log.replay_after("sessB", 2)
    assert [e.id for e in result.events] == [3, 4, 5]
    assert [e.payload["n"] for e in result.events] == [2, 3, 4]
    assert result.gap is False


async def test_event_log_detects_trim_gap(container: Container) -> None:
    # A tiny ring so older events are trimmed, making an old cursor unrecoverable.
    log = EventLog(container.redis, namespace="kinora:test:evlog3", max_len=3)
    for i in range(6):
        await log.append("sessC", {"event": "tick", "n": i})
    # Oldest retained is id 4 (ids 1..3 trimmed); resuming from id 1 is a gap.
    result = await log.replay_after("sessC", 1)
    assert result.gap is True
    assert await log.oldest_id("sessC") == 4


async def test_append_framed_injects_id(container: Container) -> None:
    log = EventLog(container.redis, namespace="kinora:test:evlog4")
    framed = await log.append_framed("sessD", {"event": "regen_done", "shot_id": "x"})
    assert framed["id"] == 1
    assert framed["shot_id"] == "x"


# --------------------------------------------------------------------------- #
# Recorder (the resumable-stream tee)
# --------------------------------------------------------------------------- #


def test_session_id_from_channel() -> None:
    assert session_id_from_channel("kinora:events:session:sess_123") == "sess_123"
    assert session_id_from_channel("kinora:events:book:b1") is None


async def test_recorder_tees_published_events_into_log(container: Container) -> None:
    log = EventLog(container.redis, namespace="kinora:test:rec")
    recorder = EventRecorder(container.redis, log)
    stop = asyncio.Event()
    task = asyncio.create_task(recorder.run(stop))
    try:
        await asyncio.sleep(0.3)  # let the psubscribe settle
        await container.redis.publish(
            session_channel("sess_rec"), {"event": "clip_ready", "shot_id": "s9"}
        )
        # Poll the log until the recorder appends (or time out).
        for _ in range(40):
            if await log.size("sess_rec") >= 1:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
    result = await log.replay_after("sess_rec", 0)
    assert any(e.payload.get("shot_id") == "s9" for e in result.events)


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #


async def test_presence_join_move_leave(container: Container) -> None:
    presence = PresenceService(container.redis, namespace="kinora:test:pres")
    await presence.join(
        "sP", participant_id="p1", user_id="u1", display="Ada", focus_word=10, mode="viewer"
    )
    roster = await presence.roster("sP")
    assert len(roster) == 1 and roster[0].display == "Ada" and roster[0].focus_word == 10

    moved = await presence.move("sP", "p1", focus_word=42, mode="director")
    assert moved is not None and moved.focus_word == 42 and moved.mode == "director"

    await presence.leave("sP", "p1")
    assert await presence.count("sP") == 0


async def test_presence_heartbeat_on_missing_is_false(container: Container) -> None:
    presence = PresenceService(container.redis, namespace="kinora:test:pres2")
    assert await presence.heartbeat("sQ", "ghost") is False


async def test_presence_fans_out_on_channel(container: Container) -> None:
    presence = PresenceService(container.redis, namespace="kinora:test:pres3")
    async with container.redis.subscribe(session_channel("sR")) as pubsub:
        await asyncio.sleep(0.1)
        await presence.join("sR", participant_id="p2", user_id="u2", display="Lin")
        message = await container.redis.next_message(pubsub, timeout=3.0)
    assert message is not None
    assert message["event"] == "presence"
    assert message["action"] == "join"
    assert message["participant"]["display"] == "Lin"


# --------------------------------------------------------------------------- #
# Connection registry
# --------------------------------------------------------------------------- #


async def test_connection_registry_counts_and_caps(container: Container) -> None:
    reg = ConnectionRegistry(
        container.redis, namespace="kinora:test:conn", max_per_session=2, max_per_user=10
    )
    async with reg.connection(session_id="sC", user_id="u1"):
        assert await reg.session_count("sC") == 1
        async with reg.connection(session_id="sC", user_id="u1"):
            assert await reg.session_count("sC") == 2
            # The third concurrent connection breaches the session cap.
            with pytest.raises(ConnectionLimitExceeded):
                async with reg.connection(session_id="sC", user_id="u1"):
                    pass
    # Both closed on exit.
    assert await reg.session_count("sC") == 0


async def test_connection_registry_reaps_stale(container: Container) -> None:
    reg = ConnectionRegistry(
        container.redis, namespace="kinora:test:conn2", stale_after_s=0.0
    )
    async with reg.connection(session_id="sD", user_id="u1"):
        # stale_after_s=0 means every member is immediately "stale"; reap clears it.
        reaped = await reg.reap_all()
        assert reaped >= 1
        assert await reg.session_count("sD") == 0


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


async def test_idempotency_replays_recorded_response(container: Container) -> None:
    store = IdempotencyStore(container.redis, namespace="kinora:test:idem")
    fp = fingerprint("POST", "/x", b'{"a":1}')
    first = await store.begin(user_id="u1", scope="comment", idem_key="k1", request_fingerprint=fp)
    assert first.proceed is True
    await store.record(
        user_id="u1", scope="comment", idem_key="k1", request_fingerprint=fp,
        status=201, body={"shot_id": "s1"},
    )
    second = await store.begin(user_id="u1", scope="comment", idem_key="k1", request_fingerprint=fp)
    assert second.replay is not None
    assert second.replay.status == 201
    assert second.replay.body == {"shot_id": "s1"}


async def test_idempotency_in_flight_conflict(container: Container) -> None:
    store = IdempotencyStore(container.redis, namespace="kinora:test:idem2")
    fp = fingerprint("POST", "/x", b"{}")
    first = await store.begin(user_id="u1", scope="s", idem_key="k2", request_fingerprint=fp)
    assert first.proceed is True
    # Second arrives before record() — the first is still pending.
    second = await store.begin(user_id="u1", scope="s", idem_key="k2", request_fingerprint=fp)
    assert second.conflict is True


async def test_idempotency_key_reuse_mismatch(container: Container) -> None:
    store = IdempotencyStore(container.redis, namespace="kinora:test:idem3")
    fp_a = fingerprint("POST", "/x", b'{"a":1}')
    fp_b = fingerprint("POST", "/x", b'{"a":2}')
    await store.begin(user_id="u1", scope="s", idem_key="k3", request_fingerprint=fp_a)
    await store.record(
        user_id="u1", scope="s", idem_key="k3", request_fingerprint=fp_a, status=200, body={}
    )
    # Same key, different body -> reuse error.
    out = await store.begin(user_id="u1", scope="s", idem_key="k3", request_fingerprint=fp_b)
    assert out.mismatch is True


async def test_idempotency_release_unwedges_pending(container: Container) -> None:
    store = IdempotencyStore(container.redis, namespace="kinora:test:idem4")
    fp = fingerprint("POST", "/x", b"{}")
    await store.begin(user_id="u1", scope="s", idem_key="k4", request_fingerprint=fp)
    await store.release(user_id="u1", scope="s", idem_key="k4")
    # After release the key is free to run again.
    out = await store.begin(user_id="u1", scope="s", idem_key="k4", request_fingerprint=fp)
    assert out.proceed is True


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #


async def test_versions_route(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "v1"
    assert any(v["version"] == "v1" for v in body["versions"])


async def test_presence_routes_roundtrip(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)

    join = await api_client.post(
        f"/api/sessions/{session_id}/presence",
        headers=auth_headers,
        json={"display": "Ada", "focus_word": 5, "mode": "viewer"},
    )
    assert join.status_code == 201, join.text
    pid = join.json()["participant_id"]

    roster = await api_client.get(
        f"/api/sessions/{session_id}/presence", headers=auth_headers
    )
    assert roster.status_code == 200
    assert roster.json()["count"] == 1

    move = await api_client.patch(
        f"/api/sessions/{session_id}/presence/{pid}",
        headers=auth_headers,
        json={"focus_word": 99},
    )
    assert move.status_code == 200 and move.json()["status"] == "ok"

    leave = await api_client.delete(
        f"/api/sessions/{session_id}/presence/{pid}", headers=auth_headers
    )
    assert leave.status_code == 200 and leave.json()["status"] == "left"


async def test_presence_join_idempotency_key_replays(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """Two POSTs with the same Idempotency-Key mint one participant, not two."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    headers = {**auth_headers, "Idempotency-Key": "join-once-abc"}
    payload = {"display": "Ada", "focus_word": 5, "mode": "viewer"}

    first = await api_client.post(
        f"/api/sessions/{session_id}/presence", headers=headers, json=payload
    )
    assert first.status_code == 201
    assert first.headers.get("Idempotent-Replayed") == "false"
    pid = first.json()["participant_id"]

    # A retry replays the *same* participant id (no second join).
    second = await api_client.post(
        f"/api/sessions/{session_id}/presence", headers=headers, json=payload
    )
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json()["participant_id"] == pid

    # Exactly one participant is present.
    roster = await api_client.get(
        f"/api/sessions/{session_id}/presence", headers=auth_headers
    )
    assert roster.json()["count"] == 1


async def test_presence_join_idempotency_key_reuse_mismatch(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """Reusing a key for a *different* body is rejected (422), never masked."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    headers = {**auth_headers, "Idempotency-Key": "join-key-x"}

    first = await api_client.post(
        f"/api/sessions/{session_id}/presence", headers=headers, json={"display": "Ada"}
    )
    assert first.status_code == 201
    second = await api_client.post(
        f"/api/sessions/{session_id}/presence", headers=headers, json={"display": "Lin"}
    )
    assert second.status_code == 422
    assert second.json()["error"]["type"] == "idempotency_key_reuse"


async def test_presence_requires_ownership(
    api_client: AsyncClient,
    container: Container,
    auth_headers: dict[str, str],
    make_user: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    other = await make_user("intruder@example.com")
    resp = await api_client.get(f"/api/sessions/{session_id}/presence", headers=other)
    assert resp.status_code == 404


async def test_stream_info_route(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    # Seed a couple of events directly into the session's log.
    log = EventLog(container.redis)
    await log.append(session_id, {"event": "clip_ready", "shot_id": "s1"})
    await log.append(session_id, {"event": "clip_ready", "shot_id": "s2"})
    resp = await api_client.get(
        f"/api/sessions/{session_id}/stream/info", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_event_id"] == 2
    assert body["retained"] == 2


async def test_events_history_pagination(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    log = EventLog(container.redis)
    for i in range(5):
        await log.append(session_id, {"event": "tick", "n": i})

    first = await api_client.get(
        f"/api/sessions/{session_id}/events/history?limit=2", headers=auth_headers
    )
    assert first.status_code == 200, first.text
    page1 = first.json()
    assert [e["payload"]["n"] for e in page1["items"]] == [0, 1]
    assert page1["has_more"] is True
    cursor = page1["next_cursor"]
    assert cursor

    second = await api_client.get(
        f"/api/sessions/{session_id}/events/history?limit=2&cursor={cursor}",
        headers=auth_headers,
    )
    page2 = second.json()
    assert [e["payload"]["n"] for e in page2["items"]] == [2, 3]


async def test_events_history_rejects_bad_cursor(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    resp = await api_client.get(
        f"/api/sessions/{session_id}/events/history?cursor=garbage", headers=auth_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "invalid_cursor"


async def test_connection_stats_route(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    resp = await api_client.get(
        f"/api/sessions/{session_id}/connections", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_connections"] == 0
    assert body["max_per_session"] >= 1


# --------------------------------------------------------------------------- #
# Per-route rate limiter (standard headers + 429)
# --------------------------------------------------------------------------- #


async def test_route_rate_limiter_emits_headers_and_429(container: Container) -> None:
    from fastapi import Response

    from app.api.errors import APIError
    from app.api.realtime.ratelimit import RateLimitPolicy, RouteRateLimiter

    limiter = RouteRateLimiter(RateLimitPolicy(scope="t_regen", capacity=2, refill_per_s=0.0))
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(container=container))

    def make_request() -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [],
            "client": ("1.2.3.4", 1),
            "app": fake_app,
        }
        return Request(scope)

    resp = Response()
    # Two allowed (capacity 2), then the third is limited.
    await limiter(make_request(), resp)
    assert resp.headers["RateLimit-Limit"] == "2"
    assert int(resp.headers["RateLimit-Remaining"]) == 1
    await limiter(make_request(), Response())
    with pytest.raises(APIError) as exc:
        await limiter(make_request(), Response())
    assert exc.value.status == 429
    assert exc.value.detail is not None and "retry_after_s" in exc.value.detail


# --------------------------------------------------------------------------- #
# Resumable SSE stream (drive the StreamingResponse body directly)
# --------------------------------------------------------------------------- #


async def test_stream_resumes_from_last_event_id(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    """A reconnect with Last-Event-ID replays the gap before tailing live."""
    book_id = await seed_owned_book(api_client, container, auth_headers)
    session_id = await _create_session(api_client, auth_headers, book_id)
    token = auth_headers["Authorization"].split(" ", 1)[1]

    # Two events landed while "disconnected" (ids 1, 2).
    log = EventLog(container.redis)
    await log.append(session_id, {"event": "clip_ready", "shot_id": "missed1"})
    await log.append(session_id, {"event": "clip_ready", "shot_id": "missed2"})

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(container=container))

    async def receive() -> dict[str, object]:
        await asyncio.get_event_loop().create_future()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/sessions/{session_id}/stream",
        "headers": [(b"last-event-id", b"1")],
        "query_string": f"token={token}".encode(),
        "client": ("test", 1),
        "server": ("test", 80),
        "scheme": "http",
        "app": fake_app,
    }
    request = Request(scope, receive)
    response = await session_stream(session_id, request, token=token, last_event_id=1)
    body = cast("AsyncGenerator[bytes | str, None]", response.body_iterator)

    seen: list[str] = []
    try:
        # Collect the retry line, the replayed event(s), and the connected comment.
        for _ in range(8):
            chunk = _as_text(await asyncio.wait_for(body.__anext__(), timeout=6.0))
            seen.append(chunk)
            if "connected" in chunk:
                break
    finally:
        await body.aclose()

    joined = "".join(seen)
    # Resuming from id 1 replays only the *newer* event (id 2 / missed2), not id 1.
    assert "missed2" in joined
    assert "id: 2" in joined
    assert "missed1" not in joined
    assert "retry:" in joined


# --------------------------------------------------------------------------- #
# Multiplayer WebSocket (resume + presence + fan-out)
# --------------------------------------------------------------------------- #


def _ws_app() -> FastAPI:
    container = build_container(build_test_settings())
    container.embedder = FakeEmbedder()
    container.comment_classifier = FakeCommentClassifier()
    container.ingest_runner = FakeIngestRunner(container)
    app = create_app()
    app.state.container = container
    app.state.run_idle_sweeper = False
    app.state.run_realtime_sweeper = False  # no recorder/sweeper noise in the test
    return app


@requires_infra
def test_live_ws_resume_and_fanout() -> None:
    import redis as redis_sync

    app = _ws_app()
    with TestClient(app) as tc:
        tc.post("/api/auth/register", json={"email": "live@example.com", "password": "password123"})
        tok = tc.post(
            "/api/auth/login", json={"email": "live@example.com", "password": "password123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}
        book_id = tc.post(
            "/api/books", headers=headers, files={"file": ("t.pdf", tiny_pdf(), "application/pdf")}
        ).json()["id"]
        session_id = tc.post(
            "/api/sessions", headers=headers, json={"book_id": book_id}
        ).json()["session_id"]

        # Seed a logged event so the WS resume (?last_event_id=0) replays it,
        # writing via the same key layout EventLog uses.
        redis_url = cf._REDIS_URL
        assert redis_url is not None
        sync = redis_sync.Redis.from_url(redis_url, decode_responses=True)
        try:
            log_key = f"kinora:evlog:{session_id}:log"
            seq_key = f"kinora:evlog:{session_id}:seq"
            sync.set(seq_key, "1")
            sync.zadd(log_key, {'1|{"event":"clip_ready","shot_id":"resumed"}': 1})

            with tc.websocket_connect(
                f"/api/ws/sessions/{session_id}/live?token={tok}&last_event_id=0"
            ) as ws:
                # Resume replay arrives first.
                replayed = ws.receive_json()
                assert replayed["shot_id"] == "resumed"
                assert replayed["id"] == 1

                # Live fan-out: a freshly published event reaches the socket.
                sync.publish(
                    session_channel(session_id),
                    json.dumps({"event": "agent_activity", "agent": "showrunner", "message": "hi"}),
                )
                live = ws.receive_json()
                assert live["event"] == "agent_activity"

                # A presence_join message fans a presence event back out.
                ws.send_json({"type": "presence_join", "display": "Ada", "focus_word": 3})
                # Drain until we see the presence event (other events may interleave).
                for _ in range(5):
                    msg = ws.receive_json()
                    if msg.get("event") == "presence":
                        assert msg["action"] == "join"
                        break
                else:
                    raise AssertionError("no presence event fanned out")
        finally:
            sync.close()
