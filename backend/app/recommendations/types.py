"""Core types for the server-side recommendations engine.

These are the plain, JSON-friendly value objects the whole recsys is built on.
Keeping them in one module (a) lets every sub-model — content similarity,
collaborative filtering, taste vectors, the blended ranker, the eval harness —
share one vocabulary, and (b) keeps the math layers pure (they consume these
dataclasses, never the ORM rows), so the unit suite can pin behaviour against
hand-built fixtures with no DB and no network.

The recsys ranks **books** (the thing a Kinora reader turns into a film) for a
**user**. The three signals it blends are documented on :class:`BlendWeights`.
A dense vector here is always the 1152-d shared image+text embedding from §8
(``app.providers.embeddings.EMBED_DIM``); content-based similarity and the taste
vector live in that one space so cosine is meaningful across them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

#: A dense embedding vector — the 1152-d shared image+text space (§8).
Vector = list[float]


class InteractionKind(StrEnum):
    """A reader's signal on a book, ordered loosely by implicit strength.

    The recsys turns each event into a positive-feedback weight (see
    :data:`DEFAULT_KIND_WEIGHTS`): a *view* is a weak signal, a *finish* or an
    explicit *like* is strong, and a *dislike* is the one negative signal
    (a negative weight that pushes the book's contribution the other way).
    """

    VIEW = "view"
    OPEN = "open"
    READ = "read"
    FINISH = "finish"
    LIKE = "like"
    BOOKMARK = "bookmark"
    SHARE = "share"
    DISLIKE = "dislike"


#: Implicit-feedback weight per interaction kind. ``dislike`` is the sole
#: negative signal. These are deliberately small integers/halves so a taste
#: vector stays interpretable and a handful of strong signals dominate a flood
#: of weak ones. Tunable via Settings (``recs_kind_weights`` overrides merge).
DEFAULT_KIND_WEIGHTS: dict[InteractionKind, float] = {
    InteractionKind.VIEW: 0.5,
    InteractionKind.OPEN: 1.0,
    InteractionKind.READ: 2.0,
    InteractionKind.FINISH: 4.0,
    InteractionKind.LIKE: 5.0,
    InteractionKind.BOOKMARK: 3.0,
    InteractionKind.SHARE: 4.0,
    InteractionKind.DISLIKE: -4.0,
}


def kind_weight(
    kind: InteractionKind, overrides: dict[InteractionKind, float] | None = None
) -> float:
    """The implicit-feedback weight for ``kind`` (overrides win over defaults)."""
    if overrides and kind in overrides:
        return overrides[kind]
    return DEFAULT_KIND_WEIGHTS[kind]


@dataclass(frozen=True, slots=True)
class Interaction:
    """One reader↔book event in the append-only interaction log.

    The CF matrix, the taste vectors, and the popularity model are all folds over
    a stream of these. ``weight`` defaults to the kind's implicit weight but can
    be overridden (e.g. dwell-scaled). ``dwell_s`` is optional engagement time.
    """

    user_id: str
    book_id: str
    kind: InteractionKind
    at: datetime
    weight: float | None = None
    dwell_s: float | None = None

    def signal(self, overrides: dict[InteractionKind, float] | None = None) -> float:
        """The effective positive/negative feedback weight for this event."""
        return self.weight if self.weight is not None else kind_weight(self.kind, overrides)


@dataclass(frozen=True, slots=True)
class BookFeatures:
    """The content-side feature row for one book (the recsys item).

    ``embedding`` is the book's canon centroid in the shared 1152-d space — the
    mean of its character/location/style entity embeddings (or a text embedding
    of its blurb as a fallback). ``tags`` carry coarse categorical signal
    (genre, era) for the lexical/diversity side; ``popularity`` is a precomputed
    global-interest score in [0, ∞) used for cold-start + a mild prior.
    """

    book_id: str
    title: str = ""
    author: str | None = None
    embedding: Vector = field(default_factory=list)
    tags: tuple[str, ...] = ()
    popularity: float = 0.0

    @property
    def has_embedding(self) -> bool:
        """True when this book carries a non-empty content embedding."""
        return bool(self.embedding) and any(x != 0.0 for x in self.embedding)


class ReasonKind(StrEnum):
    """The provenance of a recommendation — which signal put it on the list."""

    CONTENT = "content"  # similar to a book you engaged with
    COLLABORATIVE = "collaborative"  # readers like you also read it
    TASTE = "taste"  # matches your accumulated taste vector
    POPULAR = "popular"  # broadly popular (cold-start / prior)
    BOOST = "boost"  # business-rule boost (e.g. same author, new release)


@dataclass(frozen=True, slots=True)
class Reason:
    """One explainable contribution to a recommendation's score.

    ``seed_book_id`` / ``seed_title`` name the book that *drove* a content or
    collaborative reason ("because you read X"); ``contribution`` is the signed
    amount this reason added to the blended score, so reasons can be ranked by
    impact and rendered most-important-first.
    """

    kind: ReasonKind
    contribution: float
    seed_book_id: str | None = None
    seed_title: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON projection for the API contract."""
        return {
            "kind": self.kind.value,
            "contribution": round(self.contribution, 6),
            "seed_book_id": self.seed_book_id,
            "seed_title": self.seed_title,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    """A book proposed by a candidate-generation source, pre-scoring.

    ``source_scores`` carries the raw per-signal scores (content / collaborative
    / taste / popular) that fed the recall, so the scorer can blend them without
    recomputing. The same book may be proposed by several sources; the engine
    merges duplicates, unioning their source scores (see ``merge``).
    """

    book_id: str
    source_scores: dict[ReasonKind, float] = field(default_factory=dict)
    seeds: dict[ReasonKind, tuple[str, float]] = field(default_factory=dict)

    def merge(self, other: Candidate) -> Candidate:
        """Union two candidates for the same book (max per signal, keep strongest seed)."""
        if other.book_id != self.book_id:
            raise ValueError("cannot merge candidates for different books")
        scores = dict(self.source_scores)
        for k, v in other.source_scores.items():
            scores[k] = max(scores.get(k, float("-inf")), v) if k in scores else v
        seeds = dict(self.seeds)
        for k, seed in other.seeds.items():
            if k not in seeds or seed[1] > seeds[k][1]:
                seeds[k] = seed
        return Candidate(book_id=self.book_id, source_scores=scores, seeds=seeds)


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A candidate after the blended scorer ran — score + the reasons behind it."""

    book_id: str
    score: float
    reasons: tuple[Reason, ...] = ()
    vector: Vector = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A final, ranked recommendation surfaced to the reader.

    ``rank`` is 1-based after re-ranking; ``reasons`` are the impact-ordered
    explanations; ``explanation`` is the one-line natural-language summary
    (see ``app.recommendations.reasons``).
    """

    book_id: str
    rank: int
    score: float
    title: str = ""
    author: str | None = None
    reasons: tuple[Reason, ...] = ()
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON projection for the API contract."""
        return {
            "book_id": self.book_id,
            "rank": self.rank,
            "score": round(self.score, 6),
            "title": self.title,
            "author": self.author,
            "reasons": [r.to_dict() for r in self.reasons],
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class BlendWeights:
    """The linear blend weights over the recsys signals (the ranker's knobs).

    The blended score of a candidate is::

        score = w_content      · content_sim
              + w_collaborative · cf_score
              + w_taste         · taste_sim
              + w_popularity    · popularity_norm

    plus any additive business boosts. Defaults favour personalization (content +
    taste + CF) with a small popularity prior so cold tails still surface
    something sensible. All weights are non-negative.
    """

    content: float = 1.0
    collaborative: float = 1.0
    taste: float = 1.2
    popularity: float = 0.3

    def __post_init__(self) -> None:
        for name in ("content", "collaborative", "taste", "popularity"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"blend weight {name!r} must be non-negative")

    def as_map(self) -> dict[ReasonKind, float]:
        """The weights keyed by the signal's :class:`ReasonKind`."""
        return {
            ReasonKind.CONTENT: self.content,
            ReasonKind.COLLABORATIVE: self.collaborative,
            ReasonKind.TASTE: self.taste,
            ReasonKind.POPULAR: self.popularity,
        }


@dataclass(frozen=True, slots=True)
class RecsConfig:
    """End-to-end recsys tuning knobs (candidate-gen → score → re-rank).

    Bundled so the engine, eval harness, and the service all read one config.
    Defaults mirror ``app.core.config.Settings`` recs fields; the service builds
    one of these from Settings so production and tests share the same surface.
    """

    weights: BlendWeights = field(default_factory=BlendWeights)
    #: How many candidates each recall source proposes before scoring.
    candidates_per_source: int = 50
    #: Final list length after re-ranking.
    top_k: int = 20
    #: MMR diversity trade-off (1.0 = pure relevance, lower = more diverse).
    mmr_lambda: float = 0.75
    #: Recency half-life for the taste-vector decay, in days.
    taste_half_life_days: float = 30.0
    #: Number of nearest neighbours the CF models consider.
    cf_neighbors: int = 40
    #: Minimum co-occurrence count before an item-item CF edge is trusted.
    cf_min_cooccur: int = 1
    #: Popularity damping — popularity is scaled by ``log1p(pop)/log1p(pop+damp)``.
    popularity_damping: float = 10.0
    #: Per-kind weight overrides (merged over :data:`DEFAULT_KIND_WEIGHTS`).
    kind_weights: dict[InteractionKind, float] = field(default_factory=dict)


__all__ = [
    "DEFAULT_KIND_WEIGHTS",
    "BlendWeights",
    "BookFeatures",
    "Candidate",
    "Interaction",
    "InteractionKind",
    "Reason",
    "ReasonKind",
    "RecsConfig",
    "Recommendation",
    "ScoredCandidate",
    "Vector",
    "kind_weight",
]
