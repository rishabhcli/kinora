"""Scalable semantic-retrieval primitives for canon + episodic recall (kinora.md §8.2, §8.4).

The retrieval policy (§8.4) must recall the *most relevant* canon under a limited context
window — never the whole book. As books grow to novel length the candidate pool grows, so
naive top-k by cosine returns near-duplicates and wastes the token budget. This module adds
the math that makes recall both *relevant* and *diverse* under a hard ceiling:

* :func:`cosine` / :func:`normalize` — the vector primitives (1152-d shared embedding).
* :func:`mmr_rerank` — Maximal Marginal Relevance: greedily pick items that are relevant to
  the query *and* dissimilar to what's already chosen, so k slots aren't burned on dupes.
* :func:`hybrid_score` — blend dense (vector) and sparse (lexical token-overlap) signals so a
  keyword the embedding missed still surfaces.
* :func:`pack_under_budget` — fill a token ceiling greedily by score-density (value per
  token), the discrete-knapsack-ish packing the canon slice uses to stay within context.

Pure and offline — no DB, no provider. The DB ANN index (pgvector) is the *coarse* recall;
these functions are the *fine* re-rank applied to its candidates.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

_TOKEN = re.compile(r"[a-z0-9]+")


def normalize(vec: Sequence[float]) -> list[float]:
    """Return the unit vector (zero vector maps to itself)."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product; mismatched lengths compare over the shorter prefix."""
    return sum(x * y for x, y in zip(a, b, strict=False))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1] (0 when either is the zero vector)."""
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric token set (the lexical signal for hybrid scoring)."""
    return set(_TOKEN.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    """Token-set Jaccard overlap in [0, 1] (the sparse lexical similarity)."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta and not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def hybrid_score(
    query_vec: Sequence[float],
    cand_vec: Sequence[float],
    query_text: str,
    cand_text: str,
    *,
    alpha: float = 0.7,
) -> float:
    """Blend dense cosine (weight ``alpha``) with sparse lexical Jaccard (weight ``1-alpha``).

    ``alpha=1`` is pure vector recall; lowering it lets an exact keyword the embedding glossed
    over still rank — the standard hybrid-retrieval trade-off.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha must be in [0, 1]")
    dense = (cosine(query_vec, cand_vec) + 1.0) / 2.0  # map [-1,1] → [0,1]
    sparse = jaccard(query_text, cand_text)
    return alpha * dense + (1.0 - alpha) * sparse


@dataclass(frozen=True, slots=True)
class Scored(Generic[T]):
    """An item paired with its relevance score and embedding (the re-rank unit)."""

    item: T
    score: float
    vector: list[float]


def mmr_rerank(
    query_vec: Sequence[float],
    candidates: Sequence[Scored[T]],
    *,
    k: int,
    lambda_: float = 0.6,
) -> list[Scored[T]]:
    """Maximal Marginal Relevance re-rank — relevant *and* diverse top-k.

    Greedily selects the candidate maximizing
    ``λ · relevance(query) − (1−λ) · max similarity(already-selected)``.
    ``λ=1`` collapses to pure relevance (top-k cosine); lowering λ buys diversity, which is
    what keeps the canon slice from spending all k slots on near-identical prior shots.
    """
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError("lambda_ must be in [0, 1]")
    if k <= 0 or not candidates:
        return []

    pool = list(candidates)
    selected: list[Scored[T]] = []
    while pool and len(selected) < k:
        best_idx = -1
        best_mmr = -math.inf
        for idx, cand in enumerate(pool):
            relevance = cand.score
            max_sim = (
                max(cosine(cand.vector, s.vector) for s in selected) if selected else 0.0
            )
            mmr = lambda_ * relevance - (1.0 - lambda_) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx
        selected.append(pool.pop(best_idx))
    return selected


def top_k(
    query_vec: Sequence[float], candidates: Sequence[Scored[T]], *, k: int
) -> list[Scored[T]]:
    """Plain relevance top-k (the baseline MMR degrades to at ``lambda_=1``)."""
    return sorted(candidates, key=lambda c: c.score, reverse=True)[: max(0, k)]


@dataclass(frozen=True, slots=True)
class Packable(Generic[T]):
    """An item with a value (relevance) and a token cost (its weight in the budget)."""

    item: T
    value: float
    tokens: int


def pack_under_budget(
    items: Iterable[Packable[T]], *, token_budget: int
) -> list[Packable[T]]:
    """Greedily pack the highest value-density items under a token ceiling (§8.4).

    Sorts by value-per-token (a fast, near-optimal heuristic for the bounded-context
    knapsack) and takes items while they fit — the discipline that keeps each shot's canon
    slice within the model's window as the book grows.
    """
    if token_budget <= 0:
        return []
    ranked = sorted(
        items,
        key=lambda p: (p.value / p.tokens if p.tokens > 0 else math.inf),
        reverse=True,
    )
    chosen: list[Packable[T]] = []
    spent = 0
    for p in ranked:
        if spent + p.tokens <= token_budget:
            chosen.append(p)
            spent += p.tokens
    return chosen


def estimate_tokens(text: str, *, chars_per_token: float = 4.0) -> int:
    """A cheap token estimate (≈4 chars/token) for budget packing without a tokenizer."""
    return max(1, math.ceil(len(text) / chars_per_token))


def rank_by(
    items: Sequence[T], key: Callable[[T], float], *, k: int | None = None
) -> list[T]:
    """Sort ``items`` by a numeric key descending; optionally truncate to ``k``."""
    ranked = sorted(items, key=key, reverse=True)
    return ranked if k is None else ranked[: max(0, k)]


__all__ = [
    "Packable",
    "Scored",
    "cosine",
    "dot",
    "estimate_tokens",
    "hybrid_score",
    "jaccard",
    "mmr_rerank",
    "normalize",
    "pack_under_budget",
    "rank_by",
    "tokenize",
    "top_k",
]
