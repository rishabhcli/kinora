"""Spoiler-aware hybrid retrieval over the book's stores (kinora.md §8.4).

This is the assistant's recall layer. It mirrors the §8.4 retrieval policy — fetch
*only what the question needs* under a budget — but for a reader's question
instead of a beat:

1. **Candidate gather.** Ask the :class:`~app.assistant.read_model.CanonReadModel`
   for unscored spans across the requested source kinds (pages, canon, shots,
   beats), each spoiler-stamped with its beat ordinal.
2. **Spoiler gate.** Drop every span past the reader's beat ceiling (§8.5) — done
   *before* scoring so a future span can never even be ranked.
3. **Score.** Blend dense cosine (question-vector vs span-vector) with sparse
   lexical overlap via :func:`app.memory.retrieval.hybrid_score`, then nudge by a
   per-source-kind prior (a who-is question trusts canon spans more, a recap
   trusts beats/shots). When no vectors are present the score degrades to pure
   lexical — so the retriever still works with a fake embedder that returns one
   axis, and even with *no* embedder at all.
4. **Diversify.** :func:`app.memory.retrieval.mmr_rerank` to avoid burning the k
   slots on near-duplicate passages.

The embedder is the injected seam (``Embedder`` protocol); in tests it's the
conftest ``FakeEmbedder``. The module is otherwise pure — the only async work is
the read-model fetch and the (faked) embed call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.assistant.read_model import CanonReadModel
from app.assistant.spoiler import SpoilerDecision, SpoilerHorizon
from app.assistant.types import AssistantIntent, ReadingPosition, RetrievedSpan, SourceKind
from app.memory.interfaces import Embedder
from app.memory.retrieval import Scored, hybrid_score, mmr_rerank

#: Per-(intent, source-kind) multiplicative prior on the blended score. A who-is
#: question weights canon highest; a recap weights beats/shots; explain weights
#: page passages. Missing pairs default to 1.0 (no nudge).
_INTENT_KIND_PRIOR: dict[tuple[AssistantIntent, SourceKind], float] = {
    (AssistantIntent.WHO_IS, SourceKind.CANON): 1.6,
    (AssistantIntent.WHO_IS, SourceKind.PAGE): 1.0,
    (AssistantIntent.WHO_IS, SourceKind.BEAT): 0.9,
    (AssistantIntent.WHO_IS, SourceKind.SHOT): 0.7,
    (AssistantIntent.EXPLAIN, SourceKind.PAGE): 1.5,
    (AssistantIntent.EXPLAIN, SourceKind.BEAT): 1.0,
    (AssistantIntent.EXPLAIN, SourceKind.CANON): 0.9,
    (AssistantIntent.RECAP, SourceKind.BEAT): 1.5,
    (AssistantIntent.RECAP, SourceKind.SHOT): 1.2,
    (AssistantIntent.RECAP, SourceKind.PAGE): 0.9,
    (AssistantIntent.RECAP, SourceKind.CANON): 0.8,
    (AssistantIntent.STATE, SourceKind.CANON): 1.3,
    (AssistantIntent.STATE, SourceKind.BEAT): 1.1,
}


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """The retriever's output — ranked visible spans plus the spoiler decision."""

    spans: list[RetrievedSpan]
    spoiler: SpoilerDecision
    candidate_count: int = 0


@dataclass
class RetrievalConfig:
    """Tunable knobs for one retrieval (sensible defaults, all overridable)."""

    k: int = 8
    #: Dense weight in the hybrid blend (1.0 = pure vector, 0.0 = pure lexical).
    alpha: float = 0.6
    #: MMR diversity vs relevance trade-off (1.0 = pure relevance).
    mmr_lambda: float = 0.65
    #: Beats of slack to widen the spoiler window (immediate context).
    spoiler_margin: int = 0
    #: Restrict to these source kinds (None = all).
    kinds: tuple[SourceKind, ...] | None = None
    candidate_cap: int = 200


class Retriever:
    """Hybrid, spoiler-aware retriever over a :class:`CanonReadModel`.

    The embedder is optional: when absent (or when a span carries no vector) the
    score falls back to pure lexical overlap, so retrieval never *requires* a
    live embedding call. Pass the real provider in production and a fake in tests.
    """

    def __init__(
        self,
        read_model: CanonReadModel,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self._read_model = read_model
        self._embedder = embedder

    async def retrieve(
        self,
        book_id: str,
        question: str,
        position: ReadingPosition,
        *,
        intent: AssistantIntent = AssistantIntent.GENERAL,
        config: RetrievalConfig | None = None,
    ) -> RetrievalResult:
        """Retrieve the top-k spoiler-safe spans for ``question`` at ``position``."""
        cfg = config or RetrievalConfig()
        candidates = await self._read_model.candidate_spans(
            book_id, kinds=cfg.kinds, limit=cfg.candidate_cap
        )
        candidate_count = len(candidates)

        horizon = SpoilerHorizon(margin=cfg.spoiler_margin)
        # The read model resolves the canonical ceiling; the position may also
        # carry an explicit beat. Stamp the resolved ceiling back onto a position
        # copy so the gate is consistent regardless of which ordinal was supplied.
        ceiling = await self._read_model.resolve_ceiling_beat(position)
        gated = horizon.gate(
            candidates,
            position.model_copy(update={"beat_index": ceiling})
            if not position.allow_full_book
            else position,
        )
        visible = gated.kept
        if not visible:
            return RetrievalResult(spans=[], spoiler=gated, candidate_count=candidate_count)

        query_vec = await self._embed_query(question)
        scored = self._score(question, query_vec, visible, intent=intent, alpha=cfg.alpha)
        ranked = mmr_rerank(query_vec, scored, k=cfg.k, lambda_=cfg.mmr_lambda)

        out: list[RetrievedSpan] = []
        for item in ranked:
            span = item.item.model_copy(update={"score": round(item.score, 6)})
            out.append(span)
        return RetrievalResult(spans=out, spoiler=gated, candidate_count=candidate_count)

    # -- internals ---------------------------------------------------------- #

    async def _embed_query(self, question: str) -> list[float]:
        if self._embedder is None:
            return []
        try:
            vecs = await self._embedder.embed_texts([question])
        except Exception:  # noqa: BLE001 - degrade to lexical, never fail a query
            return []
        return vecs[0] if vecs else []

    def _score(
        self,
        question: str,
        query_vec: Sequence[float],
        spans: Sequence[RetrievedSpan],
        *,
        intent: AssistantIntent,
        alpha: float,
    ) -> list[Scored[RetrievedSpan]]:
        scored: list[Scored[RetrievedSpan]] = []
        for span in spans:
            span_vec = span.vector or []
            # If either side lacks a vector, fall back to pure lexical (alpha=0).
            eff_alpha = alpha if (query_vec and span_vec) else 0.0
            base = hybrid_score(
                query_vec, span_vec, question, span.text, alpha=eff_alpha
            )
            prior = _INTENT_KIND_PRIOR.get((intent, span.kind), 1.0)
            score = min(1.0, base * prior)
            scored.append(Scored(item=span, score=score, vector=span_vec))
        return scored


def merge_dedupe(spans: Sequence[RetrievedSpan]) -> list[RetrievedSpan]:
    """Drop spans with identical ``span_id`` (keep the highest-scoring), stable."""
    best: dict[str, RetrievedSpan] = {}
    order: list[str] = []
    for span in spans:
        cur = best.get(span.span_id)
        if cur is None:
            best[span.span_id] = span
            order.append(span.span_id)
        elif span.score > cur.score:
            best[span.span_id] = span
    return [best[sid] for sid in order]


__all__ = [
    "RetrievalConfig",
    "RetrievalResult",
    "Retriever",
    "merge_dedupe",
]
