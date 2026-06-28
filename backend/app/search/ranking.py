"""Ranking math: BM25 lexical scoring + reciprocal-rank fusion (RRF).

Two pure, dependency-free scorers the in-memory backend uses directly and the
Postgres backend mirrors (Postgres does its own ``ts_rank``, but the *fusion*
step — combining the lexical and vector arms — runs here for both backends so
the hybrid behaviour is identical and unit-testable offline):

* :class:`BM25` — the Okapi BM25 term-weighting model over an inverted index.
  BM25 is the modern default for lexical relevance: TF saturation (``k1``) +
  length normalization (``b``) + IDF, which beats raw TF-IDF on real corpora.
* :func:`reciprocal_rank_fusion` — combine ranked lists (lexical + dense) by
  ``Σ 1/(k + rank)``. RRF is rank-based, so it needs no score calibration
  between arms whose scores live on different scales — exactly the hybrid-search
  problem (a BM25 score and a cosine similarity are not comparable as numbers).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BM25Params:
    """Okapi BM25 hyperparameters (the standard defaults)."""

    k1: float = 1.2
    b: float = 0.75


def idf(num_docs: int, doc_freq: int) -> float:
    """BM25 inverse document frequency with the +0.5 smoothing (always ≥ 0).

    ``log(1 + (N - n + 0.5) / (n + 0.5))`` — the +1 inside the log keeps the IDF
    non-negative even for a term appearing in more than half the corpus (the
    classic Robertson-Sparck-Jones IDF can go negative, which inverts ranking).
    """
    if num_docs <= 0 or doc_freq <= 0:
        return 0.0
    return math.log(1.0 + (num_docs - doc_freq + 0.5) / (doc_freq + 0.5))


class BM25:
    """Okapi BM25 scorer over a prebuilt inverted index.

    Construct with corpus statistics (per-term document frequency, per-document
    field length, and the average length) then call :meth:`score_term` /
    :meth:`score` to weight a candidate document against query terms. The scorer
    holds no documents — the index owns posting lists and calls in.
    """

    def __init__(
        self,
        *,
        num_docs: int,
        avg_doc_len: float,
        params: BM25Params | None = None,
    ) -> None:
        self.num_docs = max(num_docs, 0)
        self.avg_doc_len = avg_doc_len if avg_doc_len > 0 else 1.0
        self.params = params or BM25Params()
        self._idf_cache: dict[int, float] = {}

    def term_idf(self, doc_freq: int) -> float:
        """Cached IDF for a term with ``doc_freq`` containing documents."""
        if doc_freq not in self._idf_cache:
            self._idf_cache[doc_freq] = idf(self.num_docs, doc_freq)
        return self._idf_cache[doc_freq]

    def score_term(self, *, tf: int, doc_len: int, doc_freq: int) -> float:
        """BM25 contribution of one term in one document.

        ``idf · (tf·(k1+1)) / (tf + k1·(1 - b + b·dl/avgdl))`` — TF saturates
        toward ``idf·(k1+1)`` so a term appearing 50× isn't 50× a single hit, and
        a long document is discounted by ``b·dl/avgdl``.
        """
        if tf <= 0:
            return 0.0
        k1, b = self.params.k1, self.params.b
        norm = 1.0 - b + b * (doc_len / self.avg_doc_len)
        denom = tf + k1 * norm
        if denom == 0.0:
            return 0.0
        return self.term_idf(doc_freq) * (tf * (k1 + 1.0)) / denom

    def score(
        self,
        *,
        term_tfs: Mapping[str, int],
        doc_len: int,
        doc_freqs: Mapping[str, int],
    ) -> float:
        """Sum BM25 contributions over the query terms present in a document."""
        total = 0.0
        for term, tf in term_tfs.items():
            df = doc_freqs.get(term, 0)
            if df > 0:
                total += self.score_term(tf=tf, doc_len=doc_len, doc_freq=df)
        return total


# --------------------------------------------------------------------------- #
# Reciprocal-rank fusion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RankedList:
    """A ranked list of document ids (best first) plus an optional fusion weight."""

    doc_ids: Sequence[str]
    weight: float = 1.0


def reciprocal_rank_fusion(
    lists: Iterable[RankedList],
    *,
    k: int = 60,
) -> list[tuple[str, float]]:
    """Fuse several ranked lists into one by reciprocal-rank fusion.

    For each list, a document at 0-based rank ``r`` contributes
    ``weight / (k + r + 1)``. The constant ``k`` (default 60, the value from the
    original RRF paper) damps the contribution of low-rank items so the head of
    each list dominates. Returns ``(doc_id, fused_score)`` sorted descending.

    RRF needs no score normalization between arms — that is its whole appeal for
    hybrid search where a BM25 score and a cosine similarity are incomparable.
    """
    fused: dict[str, float] = {}
    for ranked in lists:
        w = ranked.weight
        for rank, doc_id in enumerate(ranked.doc_ids):
            fused[doc_id] = fused.get(doc_id, 0.0) + w / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


def weighted_score_fusion(
    scored_lists: Iterable[tuple[Mapping[str, float], float]],
    *,
    normalize: bool = True,
) -> list[tuple[str, float]]:
    """Fuse score-maps by a weighted (optionally min-max normalized) sum.

    An alternative to RRF when the caller *does* want raw scores to influence the
    blend (e.g. a strong BM25 hit should outrank a weak vector hit). Min-max
    normalization per arm puts both on ``[0, 1]`` before the weighted sum so the
    arm with the larger native scale doesn't dominate by units alone.
    """
    arms: list[tuple[dict[str, float], float]] = []
    for scores, weight in scored_lists:
        adjusted = dict(scores)
        if normalize and adjusted:
            lo = min(adjusted.values())
            hi = max(adjusted.values())
            span = hi - lo
            if span > 0:
                adjusted = {d: (s - lo) / span for d, s in adjusted.items()}
            else:
                adjusted = dict.fromkeys(adjusted, 1.0)
        arms.append((adjusted, weight))

    fused: dict[str, float] = {}
    for scores, weight in arms:
        for doc_id, s in scores.items():
            fused[doc_id] = fused.get(doc_id, 0.0) + weight * s
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


__all__ = [
    "BM25",
    "BM25Params",
    "RankedList",
    "idf",
    "reciprocal_rank_fusion",
    "weighted_score_fusion",
]
