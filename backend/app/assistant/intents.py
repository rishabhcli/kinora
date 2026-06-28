"""Intent classification — what kind of question is the reader asking?

The assistant supports a small, deliberate set of intents (kinora.md §8 read
side):

* **who-is** — "who is Elsa?", "tell me about the Snow Queen" — entity lookup.
* **explain** — "what does this passage mean?", "explain this" — interpretation.
* **recap** — "what's happened so far?", "remind me", "summary" — synthesis.
* **state** — "where are they now?", "what does he have?" — current-state (§8.5).
* **general** — anything else (open grounded QA).

Classification is *rule-based and pure* — no model call — so it's deterministic
and free, and it also extracts a candidate entity name for who-is questions so
the retriever can weight that entity. An LLM classifier could later override this
behind the same return type, but the rules cover the demo intents cleanly and
keep the hot path zero-cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.assistant.types import AssistantIntent

#: Phrases that strongly imply each intent (checked in priority order).
_RECAP_CUES = (
    "what happened",
    "what has happened",
    "so far",
    "recap",
    "summary",
    "summarise",
    "summarize",
    "catch me up",
    "remind me what",
    "story so far",
    "up to now",
    "up to this point",
)
_EXPLAIN_CUES = (
    "explain",
    "what does this mean",
    "what does this passage",
    "meaning of this",
    "interpret",
    "help me understand this",
    "what is happening here",
    "what's going on here",
)
_STATE_CUES = (
    "where are they",
    "where is ",
    "what does he have",
    "what does she have",
    "what do they have",
    "right now",
    "currently",
    "at this point",
    "who is with",
)
_WHO_IS_CUES = (
    "who is ",
    "who's ",
    "who are ",
    "tell me about ",
    "what is ",
    "what's ",
    "describe ",
    "what do we know about ",
)

#: A capitalized name-ish token (avoids leading stopwords); used for who-is.
_NAME_RE = re.compile(r"\b([A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+){0,2})\b")
_STOP_NAMES = {"I", "The", "A", "An", "What", "Who", "Where", "When", "Why", "How", "Is"}


@dataclass(frozen=True, slots=True)
class IntentResult:
    """The classified intent plus any extracted entity name (who-is focus)."""

    intent: AssistantIntent
    entity_name: str | None = None
    #: Lowercase cue that fired (for observability / tests).
    matched_cue: str = ""


def classify_intent(question: str) -> IntentResult:
    """Classify a reader's question into an :class:`AssistantIntent` (pure).

    Priority: recap > explain > state > who-is > general. Recap and explain are
    checked first because their phrasing ("what happened so far", "explain this")
    can superficially look like a who-is ("what is ...").
    """
    q = question.strip()
    lowered = q.lower()

    for cue in _RECAP_CUES:
        if cue in lowered:
            return IntentResult(AssistantIntent.RECAP, matched_cue=cue)
    for cue in _EXPLAIN_CUES:
        if cue in lowered:
            return IntentResult(AssistantIntent.EXPLAIN, matched_cue=cue)
    for cue in _STATE_CUES:
        if cue in lowered:
            return IntentResult(
                AssistantIntent.STATE, entity_name=_extract_name(q), matched_cue=cue
            )
    for cue in _WHO_IS_CUES:
        if lowered.startswith(cue) or f" {cue}" in lowered:
            name = _extract_name(q)
            # "what is X" with no name is a general question, not a who-is.
            if name is not None or cue.startswith("who"):
                return IntentResult(
                    AssistantIntent.WHO_IS, entity_name=name, matched_cue=cue
                )
    return IntentResult(AssistantIntent.GENERAL, entity_name=_extract_name(q))


def _extract_name(question: str) -> str | None:
    """Pull the most likely entity name from a question (best-effort, pure).

    Prefers a capitalized multi-word phrase that isn't a leading interrogative.
    Returns ``None`` when nothing name-like is present.
    """
    candidates: list[str] = []
    for match in _NAME_RE.finditer(question):
        name = match.group(1).strip()
        head = name.split()[0]
        if head in _STOP_NAMES and " " not in name:
            continue
        # Strip a leading stopword from a multi-word match ("The Snow Queen").
        parts = name.split()
        while parts and parts[0] in _STOP_NAMES:
            parts = parts[1:]
        if parts:
            candidates.append(" ".join(parts))
    if not candidates:
        return None
    # The longest candidate is usually the full name ("Snow Queen" over "Snow").
    return max(candidates, key=len)


__all__ = ["IntentResult", "classify_intent"]
