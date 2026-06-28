"""The realtime routers — resumable SSE, presence, stats, versions (§5.1–§5.6).

Thin shells over the realtime services bundle (:mod:`app.api.realtime.services`)
and the round-1 composition container. Mounted *additively* alongside the
existing routers; none of round-1's `events`/`sessions`/`director` routes change.

Surface:

* ``GET  /sessions/{id}/stream`` — the **resumable** SSE stream. Identical event
  payloads to round-1's ``/sessions/{id}/events`` but with ``id:`` lines + a
  ``Last-Event-ID`` replay, connection-cap enforcement, and a heartbeat/lifetime
  policy. The original endpoint is kept for back-compat.
* ``GET  /sessions/{id}/stream/info`` — event-log watermarks (diagnostics).
* ``GET  /sessions/{id}/events/history`` — **cursor-paginated** replay of the
  logged §5.6 events (a refresh-survivable history, not a live stream).
* ``GET/POST/DELETE /sessions/{id}/presence`` + heartbeat/move — **multiplayer**
  presence for co-reading.
* ``GET  /sessions/{id}/connections`` — live connection + presence headcount.
* ``WS   /ws/sessions/{id}/live`` — a unified multiplayer WS: fan-out (with ids),
  client intent/seek/comment (delegated to round-1's handler), presence
  heartbeat/move, and resume via ``?last_event_id=``.
* ``GET  /versions`` — the API version + deprecation manifest.

Auth + ownership reuse the round-1 helpers via thin local copies (EventSource /
WebSocket can't set an ``Authorization`` header, so both accept ``?token=``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.realtime.connections import ConnectionLimitExceeded
from app.api.realtime.envelopes import (
    ConnectionStatsResponse,
    EventEnvelope,
    JoinPresenceRequest,
    JoinPresenceResponse,
    MovePresenceRequest,
    ParticipantView,
    PresenceAckResponse,
    PresenceRosterResponse,
    StreamInfoResponse,
)
from app.api.realtime.idempotency_dep import ReplayResponse, build_guard
from app.api.realtime.pagination import (
    CursorError,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.api.realtime.presence import Participant
from app.api.realtime.services import RealtimeServices, get_realtime, get_realtime_ws
from app.api.realtime.sse import EventStream, StreamConfig, parse_last_event_id
from app.api.realtime.versioning import version_manifest
from app.api.security import TokenError, decode_access_token
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.enums import SessionMode
from app.db.models.session import Session as SessionRow
from app.db.models.user import User
from app.db.repositories.session import SessionRepo
from app.db.repositories.user import UserRepo
from app.queue.redis_queue import book_channel, session_channel

logger = get_logger("app.api.realtime.routes")

router = APIRouter(tags=["realtime"])

#: Typed dependency resolving the realtime services bundle off ``app.state``.
RealtimeDep = Annotated[RealtimeServices, Depends(get_realtime)]


def get_container_from(request: Request) -> Container:
    from app.api.deps import get_container

    return get_container(request)


# --------------------------------------------------------------------------- #
# Auth + ownership helpers (token-in-query for EventSource/WebSocket)
# --------------------------------------------------------------------------- #


def _bearer_token(headers: Any, query_token: str | None) -> str | None:
    header = headers.get("authorization") if hasattr(headers, "get") else None
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return query_token


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


async def _owned_session_row(container: Container, user: User, session_id: str) -> SessionRow:
    async with container.session_factory() as session:
        row = await SessionRepo(session).get(session_id)
    if row is None or row.user_id != user.id:
        raise APIError("session_not_found", "no such session for this user", status=404)
    return row


def _participant_view(p: Participant) -> ParticipantView:
    return ParticipantView(**p.to_public())


# --------------------------------------------------------------------------- #
# Resumable SSE stream
# --------------------------------------------------------------------------- #


@router.get(
    "/sessions/{session_id}/stream",
    summary="Resumable SSE stream of a session's generation events (§5.6)",
    responses={401: {"description": "missing/invalid token"}, 404: {"description": "no session"}},
)
async def session_stream(
    session_id: str,
    request: Request,
    token: str | None = Query(default=None),
    last_event_id: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    """SSE with ``id:`` cursors + ``Last-Event-ID`` resume + connection caps.

    A reconnecting EventSource sends ``Last-Event-ID`` automatically (or pass
    ``?last_event_id=``); the stream replays the logged gap, then tails live. The
    payloads are byte-for-byte the round-1 §5.6 events, so a client can switch
    from ``/events`` to ``/stream`` with no parsing change.
    """
    container = get_container_from(request)
    realtime = get_realtime(request)
    user = await _user_from_token(_bearer_token(request.headers, token), container)
    row = await _owned_session_row(container, user, session_id)

    resume_from = last_event_id
    if resume_from is None:
        resume_from = parse_last_event_id(request.headers)

    channels = [session_channel(session_id), book_channel(row.book_id)]

    def subscribe() -> Any:
        return container.redis.subscribe(*channels)

    async def next_message(pubsub: Any, timeout: float) -> dict[str, Any] | None:
        message = await container.redis.next_message(pubsub, timeout=timeout)
        return message if isinstance(message, dict) else None

    stream = EventStream(
        event_log=realtime.event_log,
        log_stream=session_id,
        subscribe=subscribe,
        next_message=next_message,
        is_disconnected=request.is_disconnected,
        last_event_id=resume_from,
        config=StreamConfig(),
    )

    async def body() -> AsyncIterator[str]:
        # Enforce the per-session connection cap for the stream's lifetime.
        try:
            async with realtime.connections.connection(
                session_id=session_id, user_id=user.id
            ) as conn:
                async for frame in stream.iter_frames():
                    # Piggyback a liveness heartbeat on the SSE ping cadence.
                    if frame.startswith(": ping"):
                        await conn.heartbeat()
                    yield frame
        except ConnectionLimitExceeded as exc:
            # Stream can't raise an HTTP status post-headers; emit a typed event
            # then end so the client can surface "too many connections".
            from app.api.realtime.sse import format_event

            yield format_event(
                {"event": "stream_error", "type": "connection_limit", "scope": exc.scope},
                event="stream_error",
            )

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)


@router.get(
    "/sessions/{session_id}/stream/info",
    response_model=StreamInfoResponse,
    summary="Event-log watermarks for a session's resumable stream",
)
async def stream_info(
    session_id: str, container: ContainerDep, realtime: RealtimeDep, user: CurrentUser
) -> StreamInfoResponse:
    """Latest/oldest retained event ids — what a resume can recover."""
    await _owned_session_row(container, user, session_id)
    log = realtime.event_log
    return StreamInfoResponse(
        session_id=session_id,
        latest_event_id=await log.latest_id(session_id),
        oldest_event_id=await log.oldest_id(session_id),
        retained=await log.size(session_id),
    )


@router.get(
    "/sessions/{session_id}/events/history",
    response_model=Page[EventEnvelope],
    summary="Cursor-paginated history of logged §5.6 events",
)
async def events_history(
    session_id: str,
    container: ContainerDep,
    realtime: RealtimeDep,
    user: CurrentUser,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> Page[EventEnvelope]:
    """A refresh-survivable replay of the event log, oldest→newest, by cursor.

    The cursor encodes the last event id returned; the next page is the events
    strictly after it. Distinct from the live stream — this is for a UI that
    reloads and wants the recent history without holding a socket open.
    """
    await _owned_session_row(container, user, session_id)
    after = _decode_after(cursor, container)
    page_size = clamp_limit(limit)

    # Pull a window from the log (one extra to detect has_more cheaply).
    result = await realtime.event_log.replay_after(
        session_id, after if after is not None else 0, limit=page_size + 1
    )
    logged = result.events
    items = [EventEnvelope(id=e.id, payload=e.payload) for e in logged[:page_size]]
    has_more = len(logged) > page_size
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor({"after": items[-1].id}, secret=container.settings.jwt_secret)
    return Page[EventEnvelope](
        items=items, next_cursor=next_cursor, has_more=has_more, page_size=page_size
    )


def _decode_after(cursor: str | None, container: Container) -> int | None:
    if cursor is None:
        return None
    try:
        payload = decode_cursor(cursor, secret=container.settings.jwt_secret)
    except CursorError as exc:
        raise APIError("invalid_cursor", "malformed pagination cursor", status=422) from exc
    after = payload.get("after")
    return int(after) if isinstance(after, int) else None


# --------------------------------------------------------------------------- #
# Multiplayer presence
# --------------------------------------------------------------------------- #


@router.get(
    "/sessions/{session_id}/presence",
    response_model=PresenceRosterResponse,
    summary="Who is co-reading this session right now (§5.2)",
)
async def presence_roster(
    session_id: str, container: ContainerDep, realtime: RealtimeDep, user: CurrentUser
) -> PresenceRosterResponse:
    await _owned_session_row(container, user, session_id)
    roster = await realtime.presence.roster(session_id)
    return PresenceRosterResponse(
        session_id=session_id,
        count=len(roster),
        participants=[_participant_view(p) for p in roster],
    )


@router.post(
    "/sessions/{session_id}/presence",
    response_model=JoinPresenceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Join a shared session's presence roster (§5.2)",
    responses={
        409: {"description": "an idempotent request with this key is in flight"},
        422: {"description": "this Idempotency-Key was used for a different request"},
    },
)
async def presence_join(
    session_id: str,
    body: JoinPresenceRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
    realtime: RealtimeDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> JoinPresenceResponse:
    """Join the roster. Honours an optional ``Idempotency-Key`` header so a retry
    of the *same* join doesn't mint a second participant (§12)."""
    await _owned_session_row(container, user, session_id)
    guard = await build_guard(request, response, user_id=user.id, scope="presence_join")
    try:
        await guard.begin()
    except ReplayResponse as replay:
        return JoinPresenceResponse(**replay.body)

    try:
        participant_id = f"part_{uuid.uuid4().hex[:16]}"
        await realtime.presence.join(
            session_id,
            participant_id=participant_id,
            user_id=user.id,
            display=body.display,
            focus_word=body.focus_word,
            mode=body.mode,
        )
        roster = await realtime.presence.roster(session_id)
        result = JoinPresenceResponse(
            participant_id=participant_id,
            session_id=session_id,
            roster=[_participant_view(p) for p in roster],
        )
    except Exception:
        await guard.abort()
        raise
    await guard.record(status.HTTP_201_CREATED, result.model_dump(mode="json"))
    return result


@router.post(
    "/sessions/{session_id}/presence/{participant_id}/heartbeat",
    response_model=PresenceAckResponse,
    summary="Refresh a participant's presence TTL (§5.2)",
)
async def presence_heartbeat(
    session_id: str,
    participant_id: str,
    container: ContainerDep,
    realtime: RealtimeDep,
    user: CurrentUser,
) -> PresenceAckResponse:
    await _owned_session_row(container, user, session_id)
    alive = await realtime.presence.heartbeat(session_id, participant_id)
    return PresenceAckResponse(
        session_id=session_id,
        participant_id=participant_id,
        status="ok" if alive else "expired",
    )


@router.patch(
    "/sessions/{session_id}/presence/{participant_id}",
    response_model=PresenceAckResponse,
    summary="Move a participant's cursor / mode (§5.2)",
)
async def presence_move(
    session_id: str,
    participant_id: str,
    body: MovePresenceRequest,
    container: ContainerDep,
    realtime: RealtimeDep,
    user: CurrentUser,
) -> PresenceAckResponse:
    await _owned_session_row(container, user, session_id)
    moved = await realtime.presence.move(
        session_id, participant_id, focus_word=body.focus_word, mode=body.mode
    )
    return PresenceAckResponse(
        session_id=session_id,
        participant_id=participant_id,
        status="ok" if moved is not None else "expired",
    )


@router.delete(
    "/sessions/{session_id}/presence/{participant_id}",
    response_model=PresenceAckResponse,
    summary="Leave a shared session's presence roster (§5.2)",
)
async def presence_leave(
    session_id: str,
    participant_id: str,
    container: ContainerDep,
    realtime: RealtimeDep,
    user: CurrentUser,
) -> PresenceAckResponse:
    await _owned_session_row(container, user, session_id)
    await realtime.presence.leave(session_id, participant_id)
    return PresenceAckResponse(
        session_id=session_id, participant_id=participant_id, status="left"
    )


@router.get(
    "/sessions/{session_id}/connections",
    response_model=ConnectionStatsResponse,
    summary="Live connection + presence headcount for a session (§5.6)",
)
async def connection_stats(
    session_id: str, container: ContainerDep, realtime: RealtimeDep, user: CurrentUser
) -> ConnectionStatsResponse:
    await _owned_session_row(container, user, session_id)
    return ConnectionStatsResponse(
        session_id=session_id,
        live_connections=await realtime.connections.session_count(session_id),
        presence_count=await realtime.presence.count(session_id),
        max_per_session=realtime.connections.max_per_session,
    )


# --------------------------------------------------------------------------- #
# Versions / deprecation manifest
# --------------------------------------------------------------------------- #


@router.get("/versions", summary="API version + deprecation manifest (§12)")
async def versions() -> dict[str, Any]:
    """Enumerate live API versions and any deprecated routes (for clients)."""
    return version_manifest()


# --------------------------------------------------------------------------- #
# Unified multiplayer WebSocket
# --------------------------------------------------------------------------- #


@router.websocket("/ws/sessions/{session_id}/live")
async def session_live_ws(websocket: WebSocket, session_id: str) -> None:
    """Multiplayer WS: id'd fan-out + presence + intent/seek/comment, with resume.

    Accepts ``?last_event_id=`` to replay the logged gap on connect (the WS twin
    of SSE resume), fans every event out with its ``id`` so the client can track a
    resumable cursor, and bridges client→backend messages to round-1's handler
    plus presence heartbeat/move.
    """
    container: Container = websocket.app.state.container
    realtime = get_realtime_ws(websocket)
    params = websocket.query_params
    token = _bearer_token(websocket.headers, params.get("token"))
    try:
        user = await _user_from_token(token, container)
        row = await _owned_session_row(container, user, session_id)
    except APIError:
        await websocket.close(code=1008)
        return

    try:
        cm = realtime.connections.connection(session_id=session_id, user_id=user.id)
        conn = await cm.__aenter__()
    except ConnectionLimitExceeded:
        await websocket.close(code=1013)  # try again later
        return

    await websocket.accept()
    channels = [session_channel(session_id), book_channel(row.book_id)]
    stop = asyncio.Event()
    participant_id = f"part_{uuid.uuid4().hex[:16]}"

    # Resume: replay the logged gap before tailing live.
    resume_from = parse_last_event_id(websocket.headers, params.get("last_event_id"))
    if resume_from is not None:
        result = await realtime.event_log.replay_after(session_id, resume_from)
        if result.gap:
            with contextlib.suppress(Exception):
                await websocket.send_json({"event": "resume_gap", "from_id": resume_from})
        for logged in result.events:
            with contextlib.suppress(Exception):
                await websocket.send_json({**logged.payload, "id": logged.id})

    async def pump_events() -> None:
        async with container.redis.subscribe(*channels) as pubsub:
            while not stop.is_set():
                message = await container.redis.next_message(pubsub, timeout=1.0)
                if isinstance(message, dict):
                    await websocket.send_json(message)
                else:
                    await conn.heartbeat()
                    await realtime.presence.heartbeat(session_id, participant_id)

    async def pump_client() -> None:
        while not stop.is_set():
            raw = await websocket.receive_text()
            with contextlib.suppress(json.JSONDecodeError):
                await _handle_live_message(
                    container, realtime, session_id, row, participant_id, json.loads(raw)
                )

    forward = asyncio.create_task(pump_events())
    try:
        await pump_client()
    except WebSocketDisconnect:
        logger.info("realtime.ws_disconnect", session_id=session_id)
    finally:
        stop.set()
        forward.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await forward
        await realtime.presence.leave(session_id, participant_id)
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


async def _handle_live_message(
    container: Container,
    realtime: RealtimeServices,
    session_id: str,
    row: SessionRow,
    participant_id: str,
    payload: dict[str, Any],
) -> None:
    """Dispatch a multiplayer WS client message.

    Generation messages (``intent_update``/``seek``/``comment``) reuse round-1's
    handler verbatim; presence messages (``presence_join``/``presence_move``/
    ``presence_heartbeat``) are handled here.
    """
    kind = payload.get("type")
    if kind in {"intent_update", "seek", "comment"}:
        from app.api.routes.events import _handle_client_message

        await _handle_client_message(container, session_id, row, payload)
        return
    if kind == "presence_join":
        await realtime.presence.join(
            session_id,
            participant_id=participant_id,
            user_id=row.user_id or "",
            display=str(payload.get("display", "reader"))[:80],
            focus_word=int(payload.get("focus_word", row.focus_word) or 0),
            mode=_safe_mode(payload.get("mode")),
        )
    elif kind == "presence_move":
        await realtime.presence.move(
            session_id,
            participant_id,
            focus_word=_opt_int(payload.get("focus_word")),
            mode=_safe_mode(payload.get("mode")) if payload.get("mode") else None,
        )
    elif kind == "presence_heartbeat":
        await realtime.presence.heartbeat(session_id, participant_id)


def _safe_mode(value: Any) -> str:
    return value if value in {SessionMode.VIEWER.value, SessionMode.DIRECTOR.value} else "viewer"


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["router"]
