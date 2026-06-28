"""Reader assistant — spoiler-aware, grounded RAG Q&A over a book + its canon.

This package is the *human-facing read side* of the §8 canon-memory layer: where
``canon.query`` (§8.4) feeds a relevant slice to the Cinematographer, the reader
assistant feeds a relevant slice to a chat model that answers a reader's
question — "who is X", "explain this passage", "what happened so far" — and it
inherits §8.5's forgetting discipline as a **spoiler horizon**: only canon and
text valid up to the reader's current position is visible, so the assistant can
never reveal what happens next.

The package is layered so the deterministic core (retrieval scoring, spoiler
gating, context packing, intent classification, grounding checks, suggestion and
eval logic) is *pure and network-free* and the only seams that touch the outside
world — the chat provider, the embedder, and a canon read model over the DB —
are injected behind protocols and satisfied by fakes in tests (zero live calls,
zero credits). See ``DESIGN.md`` for the full roadmap.
"""

from __future__ import annotations

from app.assistant.types import (
    Answer,
    AssistantIntent,
    AssistantTurn,
    Citation,
    ConversationTurn,
    ReadingPosition,
    RetrievedSpan,
    SourceKind,
    StreamDelta,
    SuggestedQuestion,
)

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
