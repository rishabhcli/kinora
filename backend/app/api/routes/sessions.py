"""Session routes — the generation-on-scroll control surface (§4.3/§4.7/§4.8).

``POST /sessions`` opens a reading session (a durable ``sessions`` row + the
Scheduler's Redis control state). ``POST /sessions/{id}/intent`` is the §4
generation-on-scroll trigger: a debounced focus-word + velocity update routed
through the :class:`IntentController` into one Scheduler control tick (promote
committed shots, maintain keyframes). ``POST /sessions/{id}/seek`` cancels
distant speculation, bridges, and re-seeds. ``GET /sessions/{id}`` returns the
live control state.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.schemas import (
    CreateSessionRequest,
    IntentRequest,
    IntentResponse,
    SeekRequest,
    SeekResponse,
    SessionResponse,
)
from app.composition import Container
from app.core.logging import get_logger
from app.db.base import new_id
from app.db.models.enums import SessionMode
from app.db.models.session import Session as SessionRow
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.db.repositories.session import SessionRepo
from app.scheduler.intent import SessionNotFoundError
from app.scheduler.model import SchedulerSession

logger = get_logger("app.api.sessions")

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _parse_mode(mode: str | None) -> SessionMode:
    if mode is None:
        return SessionMode.VIEWER
    try:
        return SessionMode(mode)
    except ValueError as exc:
        raise APIError("invalid_mode", f"unknown session mode: {mode}", status=422) from exc


async def _owned_session(container: Container, user: User, session_id: str) -> SessionRow:
    """Load a session row the user owns (404 if missing / not theirs)."""
    async with container.session_factory() as session:
        row = await SessionRepo(session).get(session_id)
    # Fail closed: a session with no owner (user_id is NULL) is accessible to
    # nobody — only the matching owner may read/drive it.
    if row is None or row.user_id != user.id:
        raise APIError("session_not_found", "no such session for this user", status=404)
    return row


def _session_response(session: SchedulerSession, *, budget: float | None = None) -> SessionResponse:
    return SessionResponse(
        session_id=session.session_id,
        book_id=session.book_id,
        focus_word=session.focus_word,
        velocity_wps=session.velocity_wps,
        mode=session.mode.value,
        committed_seconds_ahead=session.committed_seconds_ahead,
        bursting=session.bursting,
        budget_remaining_s=budget if budget is not None else session.budget_remaining_s,
        inflight=session.inflight,
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SessionResponse:
    """Open a reading session against a book (Redis control state + a durable row)."""
    mode = _parse_mode(body.mode)
    # Ownership is the durable books.user_id (the source of truth); a NULL-owner
    # book belongs to nobody (fail-closed).
    async with container.session_factory() as session:
        book = await BookRepo(session).get(body.book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)

    session_id = f"sess_{new_id()[:16]}"
    # Durable row first (with the owner), so the Scheduler's mirror preserves it.
    async with container.session_factory() as session:
        await SessionRepo(session).upsert(
            session_id=session_id,
            book_id=body.book_id,
            user_id=user.id,
            focus_word=body.focus_word,
            mode=mode,
        )

    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        sched = await controller.ensure_session(
            session_id, body.book_id, focus_word=body.focus_word, mode=mode
        )
    logger.info("sessions.created", session_id=session_id, book_id=body.book_id, user_id=user.id)
    return _session_response(sched)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str, container: ContainerDep, user: CurrentUser
) -> SessionResponse:
    """Return the Scheduler's live control state for a session."""
    row = await _owned_session(container, user, session_id)
    sched = await container.scheduler_store.load(session_id)
    if sched is None:
        # Fall back to the durable row if the Redis state expired.
        sched = SchedulerSession(
            session_id=session_id,
            book_id=row.book_id,
            focus_word=row.focus_word,
            velocity_wps=row.velocity_wps,
            committed_seconds_ahead=row.committed_seconds_ahead,
            mode=row.mode,
        )
    return _session_response(sched, budget=row.budget_remaining_s)


@router.post("/{session_id}/intent", response_model=IntentResponse)
async def update_intent(
    session_id: str,
    body: IntentRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> IntentResponse:
    """Apply a debounced reading-intent update and run one control tick (§4.9)."""
    row = await _owned_session(container, user, session_id)
    mode = _parse_mode(body.mode) if body.mode is not None else None
    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        result = await controller.handle_intent_update(
            session_id,
            body.focus_word,
            body.velocity,
            mode,
            book_id=row.book_id,
        )
    tick = result.tick
    return IntentResponse(
        session_id=session_id,
        settled=result.settled,
        allow_promotion=result.allow_promotion,
        idle=tick.idle if tick else False,
        bursting=tick.bursting if tick else False,
        committed_seconds_ahead=tick.committed_seconds_ahead if tick else 0.0,
        promoted=list(tick.promoted) if tick else [],
        keyframed=list(tick.keyframed) if tick else [],
        cancelled=tick.cancelled if tick else 0,
    )


@router.post("/{session_id}/seek", response_model=SeekResponse)
async def seek(
    session_id: str,
    body: SeekRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SeekResponse:
    """Jump to a word: cancel distant work, bridge keyframe, re-seed (§4.8)."""
    await _owned_session(container, user, session_id)
    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        try:
            result = await controller.handle_seek(session_id, body.word)
        except SessionNotFoundError as exc:
            raise APIError(
                "session_not_found", "session has no live control state", status=404
            ) from exc
    return SeekResponse(
        session_id=session_id,
        word=body.word,
        cancelled=result.cancelled,
        bridge_beat=result.bridge_beat,
        committed_seconds_ahead=result.session.committed_seconds_ahead,
    )


__all__ = ["router"]
