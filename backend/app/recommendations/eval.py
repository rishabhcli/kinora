"""Offline evaluation harness for the recommendations engine.

The recsys is only credible if it is *measured*. This harness scores a ranker
against a held-out ground truth (the leave-some-out split from
:mod:`~app.recommendations.synthetic`) with the standard top-k recommendation
metrics — all pure, deterministic functions over ranked id lists + relevance
sets, so tests pin them against hand-computed values:

* **precision@k** — fraction of the top-k that are relevant.
* **recall@k** — fraction of the relevant items that made the top-k.
* **nDCG@k** — discounted cumulative gain normalized by the ideal ranking;
  rewards putting relevant items *high*, not just *present*.
* **MAP / average precision** — precision averaged at each relevant hit; the
  rank-sensitive single-number summary.
* **MRR** — mean reciprocal rank of the first relevant hit.
* **hit-rate@k** — fraction of users with at least one relevant hit.
* **catalog coverage** — fraction of the corpus that ever gets recommended
  (anti-popularity-bias check).
* **intra-list diversity** — mean pairwise *dissimilarity* of a list's items in
  the embedding space (the MMR re-rank should raise this).
* **novelty** — mean self-information (``-log2 popularity_share``) of recommended
  items; rewards surfacing the long tail.

The :class:`EvalHarness` runs a recommender (anything matching the
:class:`Recommender` protocol — the engine, or a baseline) over every user in a
:class:`~app.recommendations.synthetic.SyntheticDataset` and aggregates the
metrics into an :class:`EvalReport`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.memory.retrieval import cosine

from .types import BookFeatures, Recommendation

# --------------------------------------------------------------------------- #
# Pure ranking metrics
# --------------------------------------------------------------------------- #


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-``k`` recommendations that are relevant."""
    if k <= 0:
        return 0.0
    top = ranked[:k]
    if not top:
        return 0.0
    hits = sum(1 for item in top if item in relevant)
    return hits / min(k, len(top))


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant items that appear in the top-``k``."""
    if not relevant:
        return 0.0
    top = set(ranked[:k])
    hits = len(top & relevant)
    return hits / len(relevant)


def average_precision(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Average precision @k — precision sampled at each relevant hit (the MAP term).

    AP rewards ranking relevant items early: a hit at rank 1 contributes 1.0, a
    hit at rank 5 contributes precision-at-5, etc., averaged over the number of
    relevant items (capped at ``k``, the achievable maximum).
    """
    if not relevant or k <= 0:
        return 0.0
    score = 0.0
    hits = 0
    for i, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            hits += 1
            score += hits / i
    denom = min(len(relevant), k)
    return score / denom if denom else 0.0


def dcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Discounted cumulative gain @k with binary relevance (gain 1 / log2(rank+1))."""
    return math.fsum(
        1.0 / math.log2(i + 1) for i, item in enumerate(ranked[:k], start=1) if item in relevant
    )


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Normalized DCG @k — DCG over the ideal DCG (1.0 = perfect ranking)."""
    if not relevant or k <= 0:
        return 0.0
    dcg = dcg_at_k(ranked, relevant, k)
    ideal_hits = min(len(relevant), k)
    idcg = math.fsum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0.0 else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Reciprocal rank of the first relevant hit within the top-``k`` (0 if none)."""
    for i, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / i
    return 0.0


def intra_list_diversity(ranked: Sequence[str], features: Mapping[str, BookFeatures]) -> float:
    """Mean pairwise dissimilarity (``1 - cosine``) of a list's embeddings.

    Higher means a more varied list (the MMR re-rank should push this up). Lists
    shorter than two, or items without embeddings, contribute no pairs → 0.0.
    """
    vecs = [features[b].embedding for b in ranked if b in features and features[b].has_embedding]
    if len(vecs) < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            total += 1.0 - cosine(vecs[i], vecs[j])
            pairs += 1
    return total / pairs if pairs else 0.0


def novelty(ranked: Sequence[str], popularity_share: Mapping[str, float]) -> float:
    """Mean self-information ``-log2(share)`` of recommended items (long-tail reward).

    ``popularity_share`` maps a book to its fraction of total engagement in [0, 1].
    A widely-engaged book has low self-information (everyone sees it); a rare one
    is high-novelty. Books absent from the share map (never engaged) are maximally
    novel and are skipped from the mean to avoid an infinite term.
    """
    infos = [
        -math.log2(popularity_share[b])
        for b in ranked
        if b in popularity_share and popularity_share[b] > 0.0
    ]
    return math.fsum(infos) / len(infos) if infos else 0.0


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


class Recommender(Protocol):
    """A recommender the harness can score — the engine or any baseline."""

    def recommend_ids(self, user_id: str, *, k: int) -> list[str]:
        """Return the ranked top-``k`` book ids for a user."""
        ...


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregated offline metrics over all evaluated users (means + corpus-wide)."""

    k: int
    users: int
    precision: float
    recall: float
    ndcg: float
    map: float
    mrr: float
    hit_rate: float
    coverage: float
    diversity: float
    novelty: float

    def to_dict(self) -> dict[str, float | int]:
        """JSON projection (rounded) for logging / the API contract."""
        return {
            "k": self.k,
            "users": self.users,
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "ndcg": round(self.ndcg, 6),
            "map": round(self.map, 6),
            "mrr": round(self.mrr, 6),
            "hit_rate": round(self.hit_rate, 6),
            "coverage": round(self.coverage, 6),
            "diversity": round(self.diversity, 6),
            "novelty": round(self.novelty, 6),
        }


def popularity_shares(interactions_by_book: Mapping[str, float]) -> dict[str, float]:
    """Normalize per-book engagement counts into a share distribution in [0, 1]."""
    total = math.fsum(interactions_by_book.values())
    if total <= 0.0:
        return {}
    return {b: c / total for b, c in interactions_by_book.items()}


@dataclass(slots=True)
class EvalHarness:
    """Run a recommender over a labelled dataset and aggregate the §metrics.

    ``ground_truth`` maps each user to their held-out relevant book ids;
    ``features`` is the corpus (for diversity); ``popularity_share`` powers the
    novelty metric. Catalog coverage is measured over ``corpus_size`` (the full
    recommendable universe) so it isn't inflated by a small candidate pool.
    """

    ground_truth: Mapping[str, set[str]]
    features: Mapping[str, BookFeatures]
    popularity_share: Mapping[str, float]
    corpus_size: int

    def evaluate(self, recommender: Recommender, *, k: int = 10) -> EvalReport:
        """Score ``recommender`` at cutoff ``k`` over every labelled user."""
        precisions: list[float] = []
        recalls: list[float] = []
        ndcgs: list[float] = []
        aps: list[float] = []
        rrs: list[float] = []
        hits: list[float] = []
        diversities: list[float] = []
        novelties: list[float] = []
        recommended_overall: set[str] = set()
        evaluated = 0

        for user_id, relevant in self.ground_truth.items():
            if not relevant:
                continue
            ranked = recommender.recommend_ids(user_id, k=k)
            recommended_overall.update(ranked)
            precisions.append(precision_at_k(ranked, relevant, k))
            recalls.append(recall_at_k(ranked, relevant, k))
            ndcgs.append(ndcg_at_k(ranked, relevant, k))
            aps.append(average_precision(ranked, relevant, k))
            rrs.append(reciprocal_rank(ranked, relevant, k))
            hits.append(1.0 if any(item in relevant for item in ranked[:k]) else 0.0)
            diversities.append(intra_list_diversity(ranked, self.features))
            novelties.append(novelty(ranked, self.popularity_share))
            evaluated += 1

        coverage = len(recommended_overall) / self.corpus_size if self.corpus_size > 0 else 0.0
        return EvalReport(
            k=k,
            users=evaluated,
            precision=_mean(precisions),
            recall=_mean(recalls),
            ndcg=_mean(ndcgs),
            map=_mean(aps),
            mrr=_mean(rrs),
            hit_rate=_mean(hits),
            coverage=coverage,
            diversity=_mean(diversities),
            novelty=_mean(novelties),
        )


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------- #
# Recommender adapters (engine + baselines) over a synthetic dataset
# --------------------------------------------------------------------------- #


def book_engagement_counts(
    interactions: Sequence[EngagementLike],
) -> dict[str, float]:
    """Count net positive engagements per book (for popularity share / novelty)."""
    counts: dict[str, float] = {}
    for event in interactions:
        counts[event.book_id] = counts.get(event.book_id, 0.0) + 1.0
    return counts


class EngagementLike(Protocol):
    """The slice of an interaction the engagement counter reads."""

    book_id: str


# --------------------------------------------------------------------------- #
# Concrete recommenders for the synthetic dataset
# --------------------------------------------------------------------------- #


class EngineRecommender:
    """Adapts :class:`~app.recommendations.engine.RecommendationEngine` to the harness.

    Wraps the pure engine over a synthetic dataset's training split (held-out
    positives removed), so :meth:`recommend_ids` returns the ranked ids the
    metrics score against the held-out ground truth.
    """

    def __init__(self, engine: object, dataset: object) -> None:
        # Imported lazily to keep eval importable without the engine.
        self._engine = engine
        self._dataset = dataset
        self._training = dataset.training_interactions()  # type: ignore[attr-defined]
        self._features = dataset.features  # type: ignore[attr-defined]
        self._as_of = dataset.as_of  # type: ignore[attr-defined]

    def recommend_ids(self, user_id: str, *, k: int) -> list[str]:
        recs: list[Recommendation] = self._engine.recommend(  # type: ignore[attr-defined]
            user_id,
            interactions=self._training,
            features=self._features,
            as_of=self._as_of,
            top_k=k,
        )
        return [r.book_id for r in recs]


class PopularityRecommender:
    """The popularity baseline: rank by global engagement, ignore personalization.

    The honest floor every personalized recommender must beat — recommending the
    same head-of-catalog books to everyone (minus what the user already touched).
    """

    def __init__(
        self,
        engagement_counts: Mapping[str, float],
        engaged_by_user: Mapping[str, set[str]],
    ) -> None:
        self._ranked = [
            b for b, _ in sorted(engagement_counts.items(), key=lambda t: t[1], reverse=True)
        ]
        self._engaged_by_user = engaged_by_user

    def recommend_ids(self, user_id: str, *, k: int) -> list[str]:
        seen = self._engaged_by_user.get(user_id, set())
        out = [b for b in self._ranked if b not in seen]
        return out[:k]


class RandomRecommender:
    """A seeded random baseline (the absolute floor — should lose on every metric)."""

    def __init__(self, universe: Sequence[str], *, seed: int = 0) -> None:
        self._universe = list(universe)
        self._seed = seed

    def recommend_ids(self, user_id: str, *, k: int) -> list[str]:
        import random as _random

        rng = _random.Random(f"{self._seed}:{user_id}")
        pool = list(self._universe)
        rng.shuffle(pool)
        return pool[:k]


__all__ = [
    "EngagementLike",
    "EngineRecommender",
    "EvalHarness",
    "EvalReport",
    "PopularityRecommender",
    "RandomRecommender",
    "Recommender",
    "average_precision",
    "book_engagement_counts",
    "dcg_at_k",
    "intra_list_diversity",
    "ndcg_at_k",
    "novelty",
    "popularity_shares",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
