"""Context assembly — pack retrieved spans under a budget with stable citations.

The §8.4 discipline is "never the whole book": fit the *most useful* grounding
into a bounded context window. This module takes the retriever's ranked spans and:

1. **Packs** them under a token ceiling by value-density (reuses
   :func:`app.memory.retrieval.pack_under_budget`), so a 300-page book still
   yields a few hundred tokens of *relevant* context.
2. **Assigns stable citation markers** ``[1]..[n]`` in packed order, so the
   prompt, the model's answer, and the grounding guard all agree on what ``[3]``
   refers to. The marker map is the contract the guard checks against.

It produces an :class:`AssembledContext`: the numbered context block the prompt
embeds, plus the ``marker -> span`` map. Pure — no DB, no provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.assistant.types import Citation, RetrievedSpan
from app.memory.retrieval import Packable, estimate_tokens, pack_under_budget

#: Default token ceiling for the grounding block (leaves room for the question,
#: the system prompt, and the answer within a comfortable context window).
DEFAULT_CONTEXT_TOKENS = 1800


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """The packed, numbered grounding block plus the marker→span map."""

    #: ``[1] (p.12) "..."`` lines, one per packed span — embedded in the prompt.
    block: str
    #: ``{marker: span}`` — the citation contract the guard validates against.
    marker_to_span: dict[int, RetrievedSpan]
    #: Total estimated tokens of the packed block.
    tokens: int

    @property
    def span_ids(self) -> list[str]:
        return [s.span_id for s in self.marker_to_span.values()]

    @property
    def is_empty(self) -> bool:
        return not self.marker_to_span

    def citation_for(self, marker: int, *, quote: str = "") -> Citation | None:
        """Build a :class:`Citation` for a marker the model used, if it's valid."""
        span = self.marker_to_span.get(marker)
        if span is None:
            return None
        return Citation(
            marker=marker,
            span_id=span.span_id,
            kind=span.kind,
            locator=span.locator,
            quote=quote or _excerpt(span.text),
        )


class ContextAssembler:
    """Pack ranked spans into a numbered, budget-bounded grounding block."""

    def __init__(self, *, token_budget: int = DEFAULT_CONTEXT_TOKENS) -> None:
        if token_budget <= 0:
            raise ValueError("token_budget must be > 0")
        self._budget = token_budget

    def assemble(self, spans: list[RetrievedSpan]) -> AssembledContext:
        """Pack ``spans`` (already ranked) and number them ``[1]..[n]``."""
        if not spans:
            return AssembledContext(block="", marker_to_span={}, tokens=0)
        packables = [
            Packable(item=span, value=max(span.score, 1e-6), tokens=estimate_tokens(span.text))
            for span in spans
        ]
        chosen = pack_under_budget(packables, token_budget=self._budget)
        # Preserve the *ranked* order in the numbered block (packing reorders by
        # density); re-sort the chosen set back into the input ranking so the
        # highest-relevance span is [1].
        chosen_ids = {p.item.span_id for p in chosen}
        kept = [s for s in spans if s.span_id in chosen_ids]

        marker_to_span: dict[int, RetrievedSpan] = {}
        lines: list[str] = []
        total = 0
        for i, span in enumerate(kept, start=1):
            marker_to_span[i] = span
            line = self._format_line(i, span)
            lines.append(line)
            total += estimate_tokens(line)
        return AssembledContext(
            block="\n".join(lines), marker_to_span=marker_to_span, tokens=total
        )

    @staticmethod
    def _format_line(marker: int, span: RetrievedSpan) -> str:
        loc = f" ({span.locator})" if span.locator else ""
        text = " ".join(span.text.split())  # collapse whitespace for the prompt
        return f"[{marker}]{loc} {text}"


def _excerpt(text: str, *, max_chars: int = 160) -> str:
    """A short single-line excerpt of a span's text for a citation quote."""
    flat = " ".join(text.split())
    return flat if len(flat) <= max_chars else flat[: max_chars - 1].rstrip() + "…"


__all__ = [
    "DEFAULT_CONTEXT_TOKENS",
    "AssembledContext",
    "ContextAssembler",
]
