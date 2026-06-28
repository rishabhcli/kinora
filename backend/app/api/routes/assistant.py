"""Reader-assistant routes — grounded, spoiler-aware Q&A (kinora.md §8 read side).

* ``POST /books/{id}/ask`` — answer a reader's question at their position; returns
  the grounded answer, validated citations, and follow-up suggestions.
* ``POST /books/{id}/ask/stream`` — the same, streamed as SSE deltas for the live
  reading-room UI (token chunks, then a final ``done`` event with the answer).
* ``GET /books/{id}/suggestions`` — suggested questions for a position, no Q asked.
* ``GET /books/{id}/conversations/{conversation_id}`` — the threaded history.
* ``DELETE /books/{id}/conversations/{conversation_id}`` — clear a thread.

Ownership is enforced exactly like the rest of the book surface: the durable
``books.user_id`` is the source of truth; a book the caller doesn't own is a 404
(fail-closed). The spoiler horizon is then applied *inside* retrieval, so even an
owner can't be shown content past where they're reading.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.assistant.schemas import (
    AnswerResponse,
    AskRequest,
    ConversationResponse,
    SuggestionsResponse,
)
from app.assistant.types import AssistantTurn, ReadingPosition, StreamDelta
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.book import BookRepo

logger = get_logger("app.api.assistant")

router = APIRouter(prefix="/books", tags=["assistant"])


async def _assert_owner(container: Container, user: User, book_id: str) -> None:
    """404 unless ``user`` owns ``book_id`` (durable ``books.user_id``, fail-closed)."""
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)


def _turn_response(turn: AssistantTurn) -> AnswerResponse:
    return AnswerResponse(
        question=turn.question,
        intent=turn.intent,
        answer=turn.answer.text,
        citations=turn.answer.citations,
        citation_coverage=turn.answer.citation_coverage,
        grounded=turn.answer.grounded,
        refused=turn.answer.refused,
        suggestions=turn.suggestions,
        context_span_ids=turn.context_span_ids,
    )


@router.post("/{book_id}/ask", response_model=AnswerResponse)
async def ask(
    book_id: str,
    body: AskRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> AnswerResponse:
    """Answer a reader's question, grounded in canon up to their position (§8)."""
    await _assert_owner(container, user, book_id)
    position = body.to_position(book_id)
    conversation_id = body.conversation_id or ""
    async with container.session_factory() as session:
        assistant = container.build_assistant(session)
        turn = await assistant.ask(
            book_id, body.question, position, conversation_id=conversation_id
        )
    return _turn_response(turn)


@router.post("/{book_id}/ask/stream")
async def ask_stream(
    book_id: str,
    body: AskRequest,
    request: Request,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> StreamingResponse:
    """Stream a grounded answer as SSE deltas (token chunks, then ``done``)."""
    await _assert_owner(container, user, book_id)
    position = body.to_position(book_id)
    conversation_id = body.conversation_id or ""

    async def stream() -> AsyncIterator[str]:
        yield ": connected\n\n"
        async with container.session_factory() as session:
            assistant = container.build_assistant(session)
            async for delta in assistant.ask_stream(
                book_id, body.question, position, conversation_id=conversation_id
            ):
                if await request.is_disconnected():
                    break
                yield _delta_frame(delta)
        logger.info("assistant.stream_closed", book_id=book_id)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@router.get("/{book_id}/suggestions", response_model=SuggestionsResponse)
async def suggestions(
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    beat_index: int | None = Query(default=None, ge=0),
    word_index: int | None = Query(default=None, ge=0),
    page: int | None = Query(default=None, ge=0),
    allow_full_book: bool = Query(default=False),
) -> SuggestionsResponse:
    """Suggested questions for the reader's current position (spoiler-safe)."""
    await _assert_owner(container, user, book_id)
    position = ReadingPosition(
        book_id=book_id,
        beat_index=beat_index,
        word_index=word_index,
        page=page,
        allow_full_book=allow_full_book,
    )
    async with container.session_factory() as session:
        assistant = container.build_assistant(session)
        suggested = await assistant.suggestions_for(book_id, position, limit=6)
    return SuggestionsResponse(suggestions=suggested)


@router.get(
    "/{book_id}/conversations/{conversation_id}", response_model=ConversationResponse
)
async def get_conversation(
    book_id: str,
    conversation_id: str,
    container: ContainerDep,
    user: CurrentUser,
) -> ConversationResponse:
    """Return the (token-bounded) recent history of a conversation thread."""
    await _assert_owner(container, user, book_id)
    history = await container.conversation_memory.recall(conversation_id)
    return ConversationResponse(
        conversation_id=conversation_id,
        turns=[{"role": t.role, "content": t.content} for t in history],
    )


@router.delete("/{book_id}/conversations/{conversation_id}", status_code=204)
async def clear_conversation(
    book_id: str,
    conversation_id: str,
    container: ContainerDep,
    user: CurrentUser,
) -> None:
    """Clear a conversation thread (forget the Q&A history)."""
    await _assert_owner(container, user, book_id)
    await container.conversation_memory.clear(conversation_id)


def _delta_frame(delta: StreamDelta) -> str:
    """Serialize a :class:`StreamDelta` as one SSE frame."""
    payload: dict[str, Any] = {"type": delta.type}
    if delta.text:
        payload["text"] = delta.text
    if delta.citations:
        payload["citations"] = [c.model_dump() for c in delta.citations]
    if delta.suggestions:
        payload["suggestions"] = [s.model_dump() for s in delta.suggestions]
    if delta.answer is not None:
        payload["answer"] = delta.answer.model_dump()
    return f"event: {delta.type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


__all__ = ["router"]
