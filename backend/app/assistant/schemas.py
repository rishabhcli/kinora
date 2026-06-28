"""Transport schemas for the reader-assistant API (kept local to the package).

These are the wire contracts for ``/books/{id}/ask`` and friends. They live in
the assistant package (not the shared ``app/api/schemas.py``) so the package owns
its own surface and the only shared-file edit is appending the router. The
internal domain types (``app.assistant.types``) are reused directly where they're
already JSON-serializable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.assistant.types import (
    AssistantIntent,
    Citation,
    ReadingPosition,
    SuggestedQuestion,
)


class AskRequest(BaseModel):
    """A reader's question at a position (the spoiler horizon input)."""

    question: str = Field(min_length=1, max_length=2000)
    #: Where the reader is — drives the spoiler gate (§8.5). Defaults to start.
    beat_index: int | None = Field(default=None, ge=0)
    word_index: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=0)
    #: Set when the reader has finished the book (disables the spoiler gate).
    allow_full_book: bool = False
    #: Optional conversation id to thread follow-ups; absent = stateless turn.
    conversation_id: str | None = Field(default=None, max_length=128)

    def to_position(self, book_id: str) -> ReadingPosition:
        return ReadingPosition(
            book_id=book_id,
            beat_index=self.beat_index,
            word_index=self.word_index,
            page=self.page,
            allow_full_book=self.allow_full_book,
        )


class AnswerResponse(BaseModel):
    """The full grounded answer + citations + follow-up suggestions."""

    question: str
    intent: AssistantIntent
    answer: str
    citations: list[Citation]
    citation_coverage: float
    grounded: bool
    refused: bool
    suggestions: list[SuggestedQuestion]
    context_span_ids: list[str]


class SuggestionsResponse(BaseModel):
    """Suggested questions for a reader's current position (no question asked)."""

    suggestions: list[SuggestedQuestion]


class ConversationResponse(BaseModel):
    """A conversation's recent (token-bounded) history."""

    conversation_id: str
    turns: list[dict[str, object]]


__all__ = [
    "AnswerResponse",
    "AskRequest",
    "ConversationResponse",
    "SuggestionsResponse",
]
