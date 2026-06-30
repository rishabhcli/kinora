"""Hybrid keyword+vector recall over canon facts (kinora.md §8.2, §8.4).

The §8.4 retrieval policy must surface the *most relevant* slice of canon under a
limited context window — never the whole book. canon2 adds a fact-level hybrid
retriever on top of the pure primitives in :mod:`app.memory.retrieval`:

* a **pluggable embedder** (the :class:`~app.memory.interfaces.Embedder` protocol)
  so production uses the real 1152-d provider and tests use a deterministic
  seeded fake — no live embeddings ever;
* **hybrid scoring** that blends dense cosine with sparse lexical overlap, so an
  exact keyword the embedding glossed over still ranks (``alpha`` trade-off);
* **MMR re-rank** for relevance *and* diversity, then **dedup** that collapses
  near-identical facts (same subject/predicate or near-1.0 cosine) so k slots are
  not burned on restatements of the same canon.

The retriever is async only at the embedding boundary; the ranking math is the
pure, offline-tested core from :mod:`app.memory.retrieval`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.memory.interfaces import Embedder
from app.memory.retrieval import (
    Scored,
    cosine,
    hybrid_score,
    mmr_rerank,
)


class CanonFact(BaseModel):
    """A retrievable canon fact: a ``(subject, predicate, object)`` triple + text.

    ``text`` is the natural-language rendering the lexical signal tokenizes; it
    defaults to the triple joined, but a caller can pass a richer gloss (the prose
    bible entry) to improve recall.
    """

    fact_key: str
    subject: str
    predicate: str
    object_value: str
    text: str | None = None
    kind: str = "state"
    valid_from_beat: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def render(self) -> str:
        """The text the lexical signal tokenizes (explicit ``text`` or the triple)."""
        if self.text:
            return self.text
        return f"{self.subject} {self.predicate} {self.object_value}"


class RetrievedFact(BaseModel):
    """A scored canon fact returned by the hybrid retriever (ranked, deduped)."""

    fact: CanonFact
    score: float
    #: The fact_keys this entry absorbed during dedup (the near-duplicates it
    #: stands in for), so provenance of the collapse is visible.
    deduped: list[str] = Field(default_factory=list)


class CanonRetriever:
    """Hybrid keyword+vector recall over a pool of canon facts (§8.4).

    Stateless over an injected :class:`Embedder`. ``retrieve`` embeds the query and
    every candidate's text once, scores with :func:`hybrid_score`, re-ranks for
    diversity with MMR, then dedups near-identical facts.
    """

    def __init__(self, embedder: Embedder, *, alpha: float = 0.7) -> None:
        self._embedder = embedder
        self._alpha = alpha

    async def retrieve(
        self,
        query: str,
        candidates: list[CanonFact],
        *,
        k: int = 5,
        lambda_: float = 0.6,
        dedup_threshold: float = 0.97,
    ) -> list[RetrievedFact]:
        """Return the top-``k`` most relevant, diverse, deduped canon facts.

        Embeds query + candidates with the injected embedder, blends dense+sparse
        scores, MMR-reranks (``lambda_`` controls diversity), then collapses
        near-duplicates (cosine >= ``dedup_threshold`` or identical subject+
        predicate+object).
        """
        if not candidates or k <= 0:
            return []

        texts = [query] + [c.render() for c in candidates]
        vectors = await self._embedder.embed_texts(texts)
        query_vec = vectors[0]
        cand_vecs = vectors[1:]

        scored: list[Scored[CanonFact]] = []
        for cand, vec in zip(candidates, cand_vecs, strict=True):
            score = hybrid_score(query_vec, vec, query, cand.render(), alpha=self._alpha)
            scored.append(Scored(item=cand, score=score, vector=vec))

        # Dedup BEFORE the budgeted re-rank so the k slots are spent on distinct
        # facts, not the most-relevant restatement of one fact.
        deduped = _dedup(scored, threshold=dedup_threshold)

        # MMR over a generous window, then truncate to k (MMR already trims, but
        # keep the representative dedup record attached).
        ranked = mmr_rerank(query_vec, [d.scored for d in deduped], k=k, lambda_=lambda_)
        by_key = {d.scored.item.fact_key: d for d in deduped}
        out: list[RetrievedFact] = []
        for s in ranked:
            rec = by_key[s.item.fact_key]
            out.append(
                RetrievedFact(fact=s.item, score=s.score, deduped=list(rec.absorbed))
            )
        return out


class _DedupRecord:
    """A representative scored fact plus the keys of the near-duplicates it absorbed."""

    __slots__ = ("scored", "absorbed")

    def __init__(self, scored: Scored[CanonFact]) -> None:
        self.scored = scored
        self.absorbed: list[str] = []


def _dedup(
    scored: list[Scored[CanonFact]], *, threshold: float
) -> list[_DedupRecord]:
    """Collapse near-identical facts, keeping the higher-scored representative.

    Two facts are duplicates when their vectors' cosine ``>= threshold`` OR they
    share an identical ``(subject, predicate, object)``. Deterministic: we walk
    candidates in descending score (stable on fact_key tiebreak) and fold each new
    fact into an existing representative when it duplicates one.
    """
    order = sorted(scored, key=lambda s: (-s.score, s.item.fact_key))
    reps: list[_DedupRecord] = []
    for s in order:
        match: _DedupRecord | None = None
        for rep in reps:
            if _is_duplicate(s, rep.scored, threshold=threshold):
                match = rep
                break
        if match is None:
            reps.append(_DedupRecord(s))
        else:
            match.absorbed.append(s.item.fact_key)
    return reps


def _is_duplicate(a: Scored[CanonFact], b: Scored[CanonFact], *, threshold: float) -> bool:
    fa, fb = a.item, b.item
    if (fa.subject, fa.predicate, fa.object_value) == (
        fb.subject,
        fb.predicate,
        fb.object_value,
    ):
        return True
    return cosine(a.vector, b.vector) >= threshold


__all__ = [
    "CanonFact",
    "CanonRetriever",
    "RetrievedFact",
]
