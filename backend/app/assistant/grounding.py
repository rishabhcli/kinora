"""The hallucination guard — every claim must cite a retrieved span (kinora.md §13).

Grounding is the assistant's safety property: an answer is only trustworthy if
its claims trace back to text we actually retrieved. This guard enforces that
*after* the model replies, independent of whether the model behaved:

1. **Parse** the inline ``[n]`` markers out of the draft answer.
2. **Validate** each marker against the assembled context's ``marker -> span``
   map — a marker the model invented (``[9]`` when we only gave it 5 spans) is
   dropped, and the sentence carrying only invalid markers is flagged unsupported.
3. **Score** citation coverage: the share of factual sentences that carry at
   least one *valid* citation. This is the faithfulness metric §13 measures.
4. **Repair**, optionally: strip unsupported sentences (strict mode) or keep them
   but flag them (lenient mode, the default — better UX, full observability).

Pure and deterministic — no model call. It consumes the model's draft + the
context contract and produces a validated :class:`~app.assistant.types.Answer`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.assistant.context import AssembledContext
from app.assistant.prompts import REFUSAL_SENTINEL, is_refusal
from app.assistant.types import Answer, Citation

#: Matches one or more bracketed integer markers, e.g. "[1]" or "[2][3]".
_MARKER_RE = re.compile(r"\[(\d+)\]")
#: Sentence splitter (kept identical to the spoiler module's for consistency).
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """Guard behaviour knobs."""

    #: Drop sentences with no valid citation instead of keeping+flagging them.
    strict: bool = False
    #: Minimum coverage below which the answer is marked ``grounded=False``.
    min_coverage: float = 0.5


def parse_markers(text: str) -> list[int]:
    """Return all citation markers found in ``text``, in order, de-duplicated."""
    seen: set[int] = set()
    out: list[int] = []
    for m in _MARKER_RE.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences (markers stay attached to their sentence)."""
    return [s.strip() for s in _SENT_RE.split(text.strip()) if s.strip()]


class GroundingGuard:
    """Validate citations and compute faithfulness for a model's draft answer."""

    def __init__(self, config: GuardConfig | None = None) -> None:
        self._config = config or GuardConfig()

    def verify(
        self,
        draft: str,
        context: AssembledContext,
        *,
        declared_markers: list[int] | None = None,
    ) -> Answer:
        """Validate ``draft`` against ``context`` and return a grounded answer.

        ``declared_markers`` (from the model's JSON ``citations`` field) are merged
        with the inline-parsed markers — either source counts as a citation.
        """
        text = draft.strip()
        if not text or is_refusal(text):
            return Answer(
                text=text or REFUSAL_SENTINEL,
                grounded=False,
                refused=True,
                citation_coverage=0.0,
            )

        valid_markers = set(context.marker_to_span.keys())
        declared = set(declared_markers or [])

        sentences = split_sentences(text)
        kept_sentences: list[str] = []
        unsupported: list[str] = []
        cited_markers: list[int] = []
        supported_count = 0
        factual_count = 0

        for sentence in sentences:
            markers = set(parse_markers(sentence))
            # A sentence with no inline marker can still be covered by a declared
            # marker only if it's the *sole* sentence (whole-answer citation).
            sentence_valid = {m for m in markers if m in valid_markers}
            is_factual = _looks_factual(sentence)
            if is_factual:
                factual_count += 1
            if sentence_valid:
                supported_count += 1 if is_factual else 0
                cited_markers.extend(sorted(sentence_valid))
                kept_sentences.append(sentence)
            else:
                if is_factual:
                    unsupported.append(sentence)
                if self._config.strict and is_factual:
                    continue  # drop unsupported factual sentence
                kept_sentences.append(sentence)

        # Whole-answer fallback: a single-sentence answer with declared markers
        # but no inline markers is treated as covered by the declared markers.
        if factual_count and supported_count == 0 and declared & valid_markers:
            supported_count = factual_count
            cited_markers.extend(sorted(declared & valid_markers))

        coverage = (supported_count / factual_count) if factual_count else 1.0
        citations = self._build_citations(sorted(set(cited_markers)), context)
        final_text = " ".join(kept_sentences).strip() or text
        grounded = bool(citations) and coverage >= self._config.min_coverage

        return Answer(
            text=final_text,
            citations=citations,
            citation_coverage=round(coverage, 4),
            grounded=grounded,
            refused=False,
            unsupported_sentences=unsupported,
        )

    @staticmethod
    def _build_citations(
        markers: list[int], context: AssembledContext
    ) -> list[Citation]:
        out: list[Citation] = []
        for marker in markers:
            cite = context.citation_for(marker)
            if cite is not None:
                out.append(cite)
        return out


def _looks_factual(sentence: str) -> bool:
    """Heuristic: a sentence makes a claim worth citing (not pure meta/question).

    Refusal/uncertainty hedges and bare questions don't need a citation; anything
    with real word content does. This keeps coverage from being unfairly dragged
    down by a closing "Would you like to know more?" line.
    """
    s = sentence.strip()
    if not s or s.endswith("?"):
        return False
    lowered = s.lower()
    hedges = (
        "i can't answer",
        "i cannot answer",
        "i'm not sure",
        "based on what you've read",
        "would you like",
        "let me know",
    )
    if any(h in lowered for h in hedges):
        return False
    # Needs at least a few alphabetic words to count as a claim.
    words = re.findall(r"[a-zA-Z]+", s)
    return len(words) >= 3


__all__ = [
    "GroundingGuard",
    "GuardConfig",
    "parse_markers",
    "split_sentences",
]
