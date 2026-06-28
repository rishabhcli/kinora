"""Suggested follow-up questions — grounded in the retrieved slice (kinora.md §8).

After answering, the assistant offers a few follow-ups the reader could ask next.
To stay spoiler-safe and grounded, suggestions are built *from the same retrieved,
spoiler-gated spans* the answer used — never from the model's outside knowledge of
the book. So a suggestion can only ever point at content the reader has reached.

Generation is deterministic and pure (zero cost): it mines entity names from
``CANON`` spans for "who is X" prompts, offers a recap and an "explain" follow-up
when there's enough page context, and de-duplicates against the question just
asked. An optional LLM refinement seam can rephrase these later, but the
deterministic set is shippable and free.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.assistant.types import (
    AssistantIntent,
    RetrievedSpan,
    SourceKind,
    SuggestedQuestion,
)

#: Default number of suggestions returned.
DEFAULT_SUGGESTION_COUNT = 4


def suggest_questions(
    spans: Sequence[RetrievedSpan],
    *,
    asked: str = "",
    limit: int = DEFAULT_SUGGESTION_COUNT,
) -> list[SuggestedQuestion]:
    """Build grounded follow-up questions from the retrieved slice (pure).

    Priority: entity who-is prompts (highest-scoring canon entities first), then a
    "what's happened so far" recap, then an "explain" prompt anchored on the
    top page passage. De-duplicated against ``asked`` and each other.
    """
    asked_l = asked.strip().lower()
    seen: set[str] = set()
    out: list[SuggestedQuestion] = []

    def _add(q: SuggestedQuestion) -> None:
        key = q.text.strip().lower()
        if key and key != asked_l and key not in seen:
            seen.add(key)
            out.append(q)

    # 1. Entity-centric who-is prompts (canon spans, by score desc).
    canon = sorted(
        (s for s in spans if s.kind == SourceKind.CANON),
        key=lambda s: s.score,
        reverse=True,
    )
    for span in canon:
        name = (span.meta or {}).get("name") or _name_from_locator(span.locator)
        entity_key = (span.meta or {}).get("entity_key")
        if name:
            _add(
                SuggestedQuestion(
                    text=f"Who is {name}?",
                    intent=AssistantIntent.WHO_IS,
                    about_entity_key=entity_key,
                )
            )

    # 2. A recap (only worth offering if there is narrative context).
    if any(s.kind in (SourceKind.BEAT, SourceKind.PAGE, SourceKind.SHOT) for s in spans):
        _add(
            SuggestedQuestion(
                text="What has happened so far?", intent=AssistantIntent.RECAP
            )
        )

    # 3. Explain the most relevant passage.
    pages = [s for s in spans if s.kind == SourceKind.PAGE]
    if pages:
        top = max(pages, key=lambda s: s.score)
        loc = f" on {top.locator}" if top.locator else ""
        _add(
            SuggestedQuestion(
                text=f"Explain what's happening{loc}.", intent=AssistantIntent.EXPLAIN
            )
        )

    # 4. A state question if a location entity is in scope.
    locs = [
        s
        for s in spans
        if s.kind == SourceKind.CANON
        and (s.meta or {}).get("entity_type") == "location"
    ]
    if locs:
        name = (locs[0].meta or {}).get("name")
        if name:
            _add(
                SuggestedQuestion(
                    text=f"Where are the characters in relation to {name}?",
                    intent=AssistantIntent.STATE,
                    about_entity_key=(locs[0].meta or {}).get("entity_key"),
                )
            )

    return out[: max(0, limit)]


def _name_from_locator(locator: str) -> str | None:
    """Pull a display name out of a ``Name (kind)`` locator, if present."""
    if "(" in locator:
        return locator.split("(", 1)[0].strip() or None
    return locator.strip() or None


__all__ = ["DEFAULT_SUGGESTION_COUNT", "suggest_questions"]
