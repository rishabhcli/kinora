"""Event transport — SSE stream + WebSocket round-trips for the §5.6 channel.

``GET /sessions/{id}/events`` is a Server-Sent-Events stream that subscribes to
the session's (and its book's) Redis pub/sub channels and forwards every §5.6
event (``keyframe_ready``, ``clip_ready``, ``scene_stitched``, ``regen_done``,
``budget_low``, ``agent_activity``, ``conflict_choice``, plus ingest progress)
as a named SSE event. ``WS /ws/sessions/{id}`` is the bidirectional counterpart:
it fans the same events out to the client *and* accepts the §5.6 client→backend
messages (``intent_update``, ``seek``, ``comment``). Both authenticate via a
Bearer header or a ``?token=`` query parameter (EventSource/WebSocket cannot set
headers) and clean up their subscription on disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.api.deps import get_container
from app.api.errors import APIError
from app.api.security import TokenError, decode_access_token
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.enums import SessionMode
from app.db.models.session import Session as SessionRow
from app.db.models.user import User
from app.db.repositories.session import SessionRepo
from app.db.repositories.user import UserRepo
from app.queue.redis_queue import book_channel, library_channel, session_channel

logger = get_logger("app.api.events")

router = APIRouter(tags=["events"])

#: SSE heartbeat / disconnect-poll cadence (seconds).
KEEPALIVE_S = 15.0


async def _user_from_token(token: str | None, container: Container) -> User:
    if not token:
        raise APIError("unauthorized", "missing token", status=401)
    try:
        claims = decode_access_token(token, container.settings)
    except TokenError as exc:
        raise APIError("unauthorized", str(exc), status=401) from exc
    async with container.session_factory() as session:
        user = await UserRepo(session).get(claims.sub)
    if user is None:
        raise APIError("unauthorized", "user no longer exists", status=401)
    return user


def _bearer_token(request_headers: Any, query_token: str | None) -> str | None:
    header = request_headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return query_token


async def _owned_session_row(container: Container, user: User, session_id: str) -> SessionRow:
    async with container.session_factory() as session:
        row = await SessionRepo(session).get(session_id)
    # Fail closed: a NULL-owner session belongs to nobody (not everybody).
    if row is None or row.user_id != user.id:
        raise APIError("session_not_found", "no such session for this user", status=404)
    return row


def _sse_frame(message: dict[str, Any]) -> str:
    event = str(message.get("event", "message"))
    return f"event: {event}\ndata: {json.dumps(message, separators=(',', ':'))}\n\n"


@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    """SSE stream of this session's §5.6 generation events."""
    container = get_container(request)
    user = await _user_from_token(_bearer_token(request.headers, token), container)
    row = await _owned_session_row(container, user, session_id)
    channels = [session_channel(session_id), book_channel(row.book_id)]

    async def stream() -> AsyncIterator[str]:
        async with container.redis.subscribe(*channels) as pubsub:
            # Sent once the subscription is live so clients (and tests) can sync.
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await container.redis.next_message(pubsub, timeout=KEEPALIVE_S)
                if message is None:
                    yield ": keepalive\n\n"
                    continue
                if isinstance(message, dict):
                    yield _sse_frame(message)
        logger.info("events.sse_closed", session_id=session_id)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.get("/books/events")
async def library_events(
    request: Request,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    """SSE stream of ingest progress for the signed-in user's library (§5.1).

    The shelf subscribes here for live ``ingest_progress`` events instead of
    polling alone. Events are published on the per-user library channel while
    Phase A runs.
    """
    container = get_container(request)
    user = await _user_from_token(_bearer_token(request.headers, token), container)
    channel = library_channel(user.id)

    async def stream() -> AsyncIterator[str]:
        async with container.redis.subscribe(channel) as pubsub:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await container.redis.next_message(pubsub, timeout=KEEPALIVE_S)
                if message is None:
                    yield ": keepalive\n\n"
                    continue
                if isinstance(message, dict):
                    yield _sse_frame(message)
        logger.info("events.library_sse_closed", user_id=user.id)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.websocket("/ws/sessions/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    """Bidirectional Director channel: fan out events + accept client messages."""
    container: Container = websocket.app.state.container
    token = _bearer_token(websocket.headers, websocket.query_params.get("token"))
    try:
        user = await _user_from_token(token, container)
        row = await _owned_session_row(container, user, session_id)
    except APIError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    channels = [session_channel(session_id), book_channel(row.book_id)]
    stop = asyncio.Event()

    async def pump_events() -> None:
        async with container.redis.subscribe(*channels) as pubsub:
            while not stop.is_set():
                message = await container.redis.next_message(pubsub, timeout=1.0)
                if message is not None and isinstance(message, dict):
                    await websocket.send_json(message)

    async def pump_client() -> None:
        while not stop.is_set():
            raw = await websocket.receive_text()
            with contextlib.suppress(json.JSONDecodeError):
                await _handle_client_message(container, session_id, row, json.loads(raw))

    forward = asyncio.create_task(pump_events())
    try:
        await pump_client()
    except WebSocketDisconnect:
        logger.info("events.ws_disconnect", session_id=session_id)
    finally:
        stop.set()
        forward.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await forward


async def _handle_client_message(
    container: Container, session_id: str, row: SessionRow, payload: dict[str, Any]
) -> None:
    """Dispatch a §5.6 client→backend WebSocket message."""
    kind = payload.get("type")
    if kind == "intent_update":
        mode = payload.get("mode")
        async with container.session_factory() as session:
            controller = container.build_intent_controller(session)
            await controller.handle_intent_update(
                session_id,
                int(payload.get("focus_word", row.focus_word)),
                float(payload.get("velocity", 4.0)),
                SessionMode(mode) if mode in {"viewer", "director"} else None,
                book_id=row.book_id,
            )
    elif kind == "seek":
        async with container.session_factory() as session:
            controller = container.build_intent_controller(session)
            with contextlib.suppress(Exception):
                await controller.handle_seek(session_id, int(payload.get("word", 0)))
    elif kind == "comment":
        note = str(payload.get("note", "")).strip()
        if note:
            route = await container.classify_comment(note)
            await container.redis.publish(
                session_channel(session_id),
                {
                    "event": "agent_activity",
                    "agent": route.agent,
                    "aspect": route.aspect,
                    "message": route.message,
                    "shot_id": payload.get("shot_id"),
                },
            )


__all__ = ["router"]
