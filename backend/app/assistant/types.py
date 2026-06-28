"""Domain types for the reader assistant (kinora.md §8).

These are the JSON-serializable contracts every layer of the assistant speaks:

* :class:`ReadingPosition` — where the reader is *right now*, the input to the
  spoiler horizon (§8.5: only facts valid up to here are visible).
* :class:`RetrievedSpan` — one candidate piece of grounding (a page passage, a
  canon-entity description, or an accepted shot's narration), tagged with its
  source kind and a beat/word ordinal so the spoiler gate can include or drop it.
* :class:`Citation` — a retrieved span that an answer actually cited, with the
  ``[n]`` marker the model used.
* :class:`Answer` — the grounded answer plus its citations and a faithfulness
  signal (did every sentence cite something we retrieved?).
* :class:`AssistantTurn` — the full record of one Q&A exchange (question +
  answer + intent + suggestions), the unit conversation memory stores.

Everything here is pure data — no DB rows, no providers. The DB read model
(``read_model.py``) projects ORM rows into :class:`RetrievedSpan` s; the chat
seam (``synth.py``) turns the assembled context into an :class:`Answer`.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SourceKind(enum.StrEnum):
    """Where a retrieved span came from — drives weighting and citation labels.

    * ``PAGE`` — a passage of the book's extracted text (the primary ground).
    * ``CANON`` — a canon entity's description (character / location / prop /
      style), resolved *as of* the reader's beat (§8.1).
    * ``SHOT`` — an accepted shot's narration / prompt (the episodic store, §8.2)
      — "what the film said here", useful for "what happened" recaps.
    * ``BEAT`` — a beat summary (the planning atom, §4.2), a compact recap source.
    """

    PAGE = "page"
    CANON = "canon"
    SHOT = "shot"
    BEAT = "beat"


class AssistantIntent(enum.StrEnum):
    """The reader's question intent (drives prompt shape + retrieval emphasis)."""

    #: "Who is Elsa?", "tell me about the castle" — entity-centric lookup.
    WHO_IS = "who_is"
    #: "Explain this passage", "what does this mean" — passage interpretation.
    EXPLAIN = "explain"
    #: "What happened so far?", "recap" — chronological synthesis up to position.
    RECAP = "recap"
    #: "Where are they?", "what does she have?" — current-state question (§8.5).
    STATE = "state"
    #: Anything else — open grounded question answering.
    GENERAL = "general"


class ReadingPosition(BaseModel):
    """The reader's current position — the input to the spoiler horizon (§8.5).

    At least one of the ordinals should be set. ``beat_index`` is the canonical
    spoiler ceiling (canon validity is keyed on it); ``word_index`` and ``page``
    are softer page-text ceilings the read model can map to a beat. When nothing
    is set the position is treated as the *start* of the book (most conservative:
    reveal nothing) unless ``allow_full_book`` is set (e.g. the reader finished).
    """

    book_id: str
    beat_index: int | None = Field(default=None, ge=0)
    word_index: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=0)
    #: When true the spoiler gate is disabled (reader has finished the book).
    allow_full_book: bool = False

    @property
    def is_unset(self) -> bool:
        """True when no positional ordinal is known (treat as book start)."""
        return self.beat_index is None and self.word_index is None and self.page is None


class RetrievedSpan(BaseModel):
    """One candidate grounding span scored against the reader's question.

    ``ordinal`` is the beat ordinal (or a best-effort beat mapping for a page
    span) used by the spoiler gate: a span whose ``ordinal`` exceeds the reader's
    beat ceiling is *future* and is dropped (§8.5). ``score`` is filled by the
    retriever; ``vector`` (optional) supports the MMR diversity re-rank.
    """

    span_id: str
    kind: SourceKind
    text: str
    ordinal: int = Field(default=0, ge=0)
    #: Human-readable provenance label, e.g. "p.12" / "Elsa (character)".
    locator: str = ""
    #: Optional dense embedding for MMR diversity (1152-d shared space).
    vector: list[float] | None = None
    #: Filled by the retriever — the blended relevance score in [0, 1].
    score: float = 0.0
    #: Optional structured extras (page number, entity_key, shot_id, ...).
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()


class Citation(BaseModel):
    """A retrieved span an answer actually cited, with its ``[n]`` marker."""

    marker: int = Field(ge=1)
    span_id: str
    kind: SourceKind
    locator: str = ""
    #: The cited excerpt (possibly a trimmed quote of the span text).
    quote: str = ""


class Answer(BaseModel):
    """A grounded answer, its citations, and faithfulness signals.

    ``citation_coverage`` is the share of the answer's sentences that carry at
    least one citation marker — the hallucination guard's headline metric (§13
    in spirit: faithfulness is measured, not assumed). ``refused`` is set when the
    assistant declined because the retrieved context did not support an answer
    (or the only support was past the spoiler horizon).
    """

    text: str
    citations: list[Citation] = Field(default_factory=list)
    citation_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    grounded: bool = True
    refused: bool = False
    #: Unsupported sentences the guard stripped or flagged (for observability).
    unsupported_sentences: list[str] = Field(default_factory=list)


class SuggestedQuestion(BaseModel):
    """A suggested follow-up question grounded in the retrieved slice."""

    text: str
    intent: AssistantIntent = AssistantIntent.GENERAL
    #: Optional entity_key the suggestion is about (drives a focused re-ask).
    about_entity_key: str | None = None


class ConversationTurn(BaseModel):
    """One stored exchange in a conversation's history (the memory unit)."""

    role: str  # "user" | "assistant"
    content: str
    #: Coarse token estimate, used to bound the rolling window.
    tokens: int = 0


class AssistantTurn(BaseModel):
    """The full record of one Q&A exchange (returned by the service)."""

    question: str
    intent: AssistantIntent
    answer: Answer
    suggestions: list[SuggestedQuestion] = Field(default_factory=list)
    #: The spans that were assembled into the prompt (for debugging / UI).
    context_span_ids: list[str] = Field(default_factory=list)


class StreamDelta(BaseModel):
    """One streamed chunk of an answer (the SSE wire unit).

    ``type`` is one of ``"token"`` (a text delta), ``"citations"`` (final
    citation list once the draft is parsed), ``"suggestions"`` (follow-ups), or
    ``"done"`` (terminal, carries the finished :class:`Answer`).
    """

    type: str
    text: str = ""
    citations: list[Citation] = Field(default_factory=list)
    suggestions: list[SuggestedQuestion] = Field(default_factory=list)
    answer: Answer | None = None


__all__ = [
    "Answer",
    "AssistantIntent",
    "AssistantTurn",
    "Citation",
    "ConversationTurn",
    "ReadingPosition",
    "RetrievedSpan",
    "SourceKind",
    "StreamDelta",
    "SuggestedQuestion",
]
