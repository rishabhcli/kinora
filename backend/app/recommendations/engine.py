"""The recommendation engine — candidate-generation → scoring → re-ranking.

This is the pure orchestrator that ties the sub-models together into the staged
pipeline the design calls for (mirroring the §8.4 retrieval policy: cheap broad
recall, then a fine, budget-aware re-rank):

1. **Candidate generation** — three recall sources propose books for the user:
   * content (`similarity.content_candidates`) from their engaged books,
   * collaborative (item-item + user-item `collaborative` models),
   * popularity (`coldstart`) as both a cold-start floor and a mild prior.
   The same book proposed by several sources is merged, unioning its per-signal
   scores so the scorer sees all the evidence at once.

2. **Scoring** — `ranker.score_candidates` blends the per-signal scores into one
   score with explainable reasons + additive business boosts.

3. **Re-ranking** — `ranker.diversify` (MMR) trims to ``top_k`` for relevance
   *and* diversity, then `ranker.to_recommendations` assigns ranks + explanations.

The engine is **pure**: it takes the user's interactions, the full interaction
log (already scoped by the caller), and the book feature corpus as plain data
and returns a ranked list. The async, DB-backed `RecommendationService`
(`store.py`) is a thin shell that loads those inputs and calls this. Cold users
fall through to popularity automatically; cold books surface via content +
popularity even before they have readers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from . import coldstart, ranker, similarity
from .collaborative import InteractionMatrix, ItemItemCF, UserUserCF
from .taste import TasteModel, engaged_book_weights, taste_similarity
from .types import (
    BlendWeights,
    BookFeatures,
    Candidate,
    Interaction,
    ReasonKind,
    Recommendation,
    RecsConfig,
)

#: Engagement weight above which a book counts as "loved" (drives boost rules).
_LOVED_THRESHOLD = 3.0


class RecommendationEngine:
    """Pure candidate-gen → score → re-rank pipeline over in-memory inputs."""

    def __init__(self, config: RecsConfig | None = None) -> None:
        self._config = config or RecsConfig()

    @property
    def config(self) -> RecsConfig:
        return self._config

    def recommend(
        self,
        user_id: str,
        *,
        interactions: Sequence[Interaction],
        features: dict[str, BookFeatures],
        as_of: datetime,
        top_k: int | None = None,
        boosts: bool = True,
    ) -> list[Recommendation]:
        """Recommend books for ``user_id`` from the interaction log + corpus."""
        cfg = self._config
        k = top_k if top_k is not None else cfg.top_k
        kw = cfg.kind_weights or None

        user_events = [e for e in interactions if e.user_id == user_id]
        engaged_weights = engaged_book_weights(
            user_events, as_of=as_of, half_life_days=cfg.taste_half_life_days, kind_weights=kw
        )
        engaged_ids = set(engaged_weights) | {e.book_id for e in user_events}

        # ---- Candidate generation (merge by book id) ------------------- #
        candidates: dict[str, Candidate] = {}

        # Content recall from the user's engaged books (per-seed attribution).
        # ``exclude=engaged_ids`` keeps already-interacted books (incl. disliked
        # ones, which aren't positive seeds) out of the candidate set.
        for book_id, sim, seed in similarity.content_candidates(
            engaged_weights,
            features,
            k=cfg.candidates_per_source,
            min_score=0.0,
            exclude=engaged_ids,
        ):
            self._add(candidates, book_id, ReasonKind.CONTENT, sim, seed)

        # Collaborative recall (item-item primary, user-user complement).
        matrix = InteractionMatrix.from_interactions(interactions, kind_weights=kw)
        item_cf = ItemItemCF(matrix, min_cooccur=cfg.cf_min_cooccur, neighbors=cfg.cf_neighbors)
        for hit in item_cf.recommend(user_id, k=cfg.candidates_per_source, exclude=engaged_ids):
            self._add(
                candidates, hit.book_id, ReasonKind.COLLABORATIVE, hit.score, hit.seed_book_id
            )
        user_cf = UserUserCF(matrix, neighbors=cfg.cf_neighbors)
        for hit in user_cf.recommend(user_id, k=cfg.candidates_per_source, exclude=engaged_ids):
            # Only deepen an existing collaborative score; user-user is the weaker
            # complement, so take the max rather than letting it dominate.
            self._add(
                candidates, hit.book_id, ReasonKind.COLLABORATIVE, hit.score, hit.seed_book_id
            )

        # Taste signal: score every already-proposed candidate against the taste
        # vector (cheap; reinforces personalization without a fourth recall).
        taste_vec = (
            TasteModel(half_life_days=cfg.taste_half_life_days)
            .build(user_events, features, as_of=as_of, kind_weights=kw)
            .vector()
        )
        if taste_vec:
            for book_id in list(candidates):
                feat = features.get(book_id)
                if feat is not None:
                    sim = taste_similarity(taste_vec, feat)
                    if sim > 0.0:
                        self._add(candidates, book_id, ReasonKind.TASTE, sim, None)

        # Popularity: cold-start floor + a small prior on every candidate.
        pop = coldstart.PopularityModel.from_interactions(
            interactions,
            as_of=as_of,
            half_life_days=cfg.taste_half_life_days,
            damping=cfg.popularity_damping,
            kind_weights=kw,
            feature_prior=features,
        )
        for book_id in list(candidates):
            self._add(candidates, book_id, ReasonKind.POPULAR, pop.score(book_id), None)
        # If recall is thin (cold user / cold corpus), fill from popularity.
        if len(candidates) < k:
            for book_id, score in coldstart.popularity_candidates(
                pop,
                k=cfg.candidates_per_source,
                exclude=engaged_ids,
                universe=features.keys(),
            ):
                self._add(candidates, book_id, ReasonKind.POPULAR, score, None)

        if not candidates:
            return []

        # ---- Scoring ---------------------------------------------------- #
        loved = frozenset(b for b, w in engaged_weights.items() if w >= _LOVED_THRESHOLD)
        rules = ranker.default_boost_rules() if boosts else ()

        def ctx_for(book_id: str) -> ranker.BoostContext:
            return ranker.BoostContext(
                book=features.get(book_id, BookFeatures(book_id=book_id)),
                features=features,
                engaged_book_ids=frozenset(engaged_ids),
                loved_book_ids=loved,
            )

        scored = ranker.score_candidates(
            candidates.values(),
            cfg.weights,
            features=features,
            boost_rules=rules,
            boost_ctx=ctx_for,
        )

        # ---- Re-ranking (MMR diversity) + projection -------------------- #
        diversified = ranker.diversify(scored, k=k, mmr_lambda=cfg.mmr_lambda)
        return ranker.to_recommendations(diversified, features=features)

    @staticmethod
    def _add(
        bucket: dict[str, Candidate],
        book_id: str,
        kind: ReasonKind,
        score: float,
        seed: str | None,
    ) -> None:
        seeds = {kind: (seed, score)} if seed else {}
        incoming = Candidate(book_id=book_id, source_scores={kind: score}, seeds=seeds)
        bucket[book_id] = bucket[book_id].merge(incoming) if book_id in bucket else incoming


def make_config_from_settings(settings: object) -> RecsConfig:
    """Build a :class:`RecsConfig` from an ``app.core.config.Settings``-like object.

    Reads the ``recs_*`` fields if present (additive Settings knobs), falling back
    to :class:`RecsConfig` defaults so the engine works with a bare Settings too.
    """

    def g(name: str, default: float | int) -> float | int:
        value = getattr(settings, name, default)
        return value if isinstance(value, int | float) else default

    weights = BlendWeights(
        content=float(g("recs_weight_content", 1.0)),
        collaborative=float(g("recs_weight_collaborative", 1.0)),
        taste=float(g("recs_weight_taste", 1.2)),
        popularity=float(g("recs_weight_popularity", 0.3)),
    )
    return RecsConfig(
        weights=weights,
        candidates_per_source=int(g("recs_candidates_per_source", 50)),
        top_k=int(g("recs_top_k", 20)),
        mmr_lambda=float(g("recs_mmr_lambda", 0.75)),
        taste_half_life_days=float(g("recs_taste_half_life_days", 30.0)),
        cf_neighbors=int(g("recs_cf_neighbors", 40)),
        cf_min_cooccur=int(g("recs_cf_min_cooccur", 1)),
        popularity_damping=float(g("recs_popularity_damping", 10.0)),
    )


__all__ = [
    "RecommendationEngine",
    "make_config_from_settings",
]
