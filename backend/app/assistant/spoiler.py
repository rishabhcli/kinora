"""The spoiler horizon — never reveal anything past the reader's position (§8.5).

§8.5 makes "timely forgetting" a property of *beat-interval validity*: a retired
fact drops out of forward retrieval so a later shot can't accidentally recall it.
The reader assistant turns that same mechanism *forward in time*: a reader at
beat 30 must not be told what happens at beat 50. So retrieval is gated by a
**spoiler ceiling** — the reader's current beat ordinal — and any candidate span
whose story-ordinal exceeds the ceiling is *future* and is dropped before it can
reach the model.

This module is pure: it computes the ceiling from a :class:`ReadingPosition` and
filters a sequence of :class:`RetrievedSpan` s. Resolving a *page* or *word*
position to a beat ordinal needs the DB (the read model does that and stamps each
span's ``ordinal``); here we only compare ordinals against a known ceiling, which
keeps the gate trivially testable and impossible to bypass by a retrieval bug.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.assistant.types import ReadingPosition, RetrievedSpan

#: When a position is entirely unknown we reveal nothing past the very start —
#: the safest default. A reader who explicitly finished sets ``allow_full_book``.
_UNSET_CEILING = 0

#: A practically-infinite ceiling for the finished-book case.
_OPEN_CEILING = 1 << 62


@dataclass(frozen=True, slots=True)
class SpoilerDecision:
    """The outcome of gating one candidate set (for observability / tests)."""

    ceiling: int
    kept: list[RetrievedSpan]
    dropped: list[RetrievedSpan]

    @property
    def drop_count(self) -> int:
        return len(self.dropped)


class SpoilerHorizon:
    """Compute a beat ceiling from a reading position and gate candidate spans.

    The ceiling is *inclusive*: a span at exactly the reader's beat is visible
    (the reader is currently there), a span at ceiling+1 is not. ``margin`` lets
    a caller widen the window by a few beats (e.g. to include the immediate
    context of the sentence the reader is on) without exposing the far future —
    it defaults to 0 (strict).
    """

    def __init__(self, *, margin: int = 0) -> None:
        if margin < 0:
            raise ValueError("margin must be >= 0")
        self._margin = margin

    def ceiling_for(self, position: ReadingPosition) -> int:
        """Resolve a :class:`ReadingPosition` to an inclusive beat ceiling.

        Precedence: an explicit ``beat_index`` wins (canon validity is keyed on
        it); otherwise the read model is expected to have resolved page/word into
        ``beat_index`` already, so an unset beat with no resolution means "start
        of book" (ceiling 0) unless the reader finished the book.
        """
        if position.allow_full_book:
            return _OPEN_CEILING
        if position.beat_index is not None:
            return position.beat_index + self._margin
        # No beat resolution available: be conservative.
        return _UNSET_CEILING + self._margin

    def is_visible(self, span: RetrievedSpan, ceiling: int) -> bool:
        """True when ``span`` is at or before the ceiling (not a future spoiler)."""
        return span.ordinal <= ceiling

    def gate(
        self, spans: Iterable[RetrievedSpan], position: ReadingPosition
    ) -> SpoilerDecision:
        """Partition ``spans`` into visible / dropped against the position's ceiling."""
        ceiling = self.ceiling_for(position)
        kept: list[RetrievedSpan] = []
        dropped: list[RetrievedSpan] = []
        for span in spans:
            (kept if self.is_visible(span, ceiling) else dropped).append(span)
        return SpoilerDecision(ceiling=ceiling, kept=kept, dropped=dropped)

    def filter(
        self, spans: Sequence[RetrievedSpan], position: ReadingPosition
    ) -> list[RetrievedSpan]:
        """Convenience: return only the visible spans (the gate's ``kept`` list)."""
        return self.gate(spans, position).kept


def redact_future_text(text: str, *, marker: str = "[…]") -> str:
    """Best-effort scrub of obvious forward-looking spoiler phrasing in free text.

    The structural gate (:meth:`SpoilerHorizon.gate`) is the real defence — it
    removes future *spans* entirely. This is a thin, conservative second pass for
    a span that straddles the ceiling: it never deletes content, it only replaces
    a sentence that *explicitly forecasts* with the marker, so a borderline page
    passage doesn't leak "later, she would die" into the model's context.
    """
    forecast_cues = (
        "later he would",
        "later she would",
        "later they would",
        "would later",
        "in the end,",
        "it would turn out",
        "as we shall see",
        "little did",
    )
    out: list[str] = []
    for sentence in _split_sentences(text):
        lowered = sentence.lower()
        if any(cue in lowered for cue in forecast_cues):
            out.append(marker)
        else:
            out.append(sentence)
    return " ".join(s for s in out if s).strip()


def _split_sentences(text: str) -> list[str]:
    """Cheap sentence split (mirrors the synthesizer's coverage splitter)."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


__all__ = ["SpoilerDecision", "SpoilerHorizon", "redact_future_text"]
