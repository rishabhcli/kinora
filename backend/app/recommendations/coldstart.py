"""Cold-start handling — sensible recommendations with little or no history.

CF and the taste vector both need *signal*: a brand-new reader has no engaged
books, and a freshly-ingested book has no readers. Cold-start is the graceful
fallback for both ends:

* **Popularity model** (:class:`PopularityModel`) — a global-interest score per
  book, folded from the interaction log (engagement-weighted, recency-decayed so
  a once-trending book fades) blended with any precomputed ``BookFeatures``
  popularity prior. With *no* personal signal at all, the recsys returns the
  most popular books the user hasn't seen — never an empty list.

* **Content fallback** — when the user has *some* engagement but it's too sparse
  for CF (no co-occurring readers yet), recall leans entirely on content
  similarity to their handful of books. This is handled by the engine wiring;
  the helper here decides *when* a user is cold for CF vs content.

Popularity is **damped** (``log1p``-shaped) so a runaway-popular title doesn't
swamp the blended ranker — it's a prior, not the whole story. Pure + offline;
tests pin the decay and damping.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime

from .taste import decay_factor
from .types import BookFeatures, Interaction, InteractionKind


def damp_popularity(raw: float, *, damping: float) -> float:
    """Squash a raw popularity mass into a gently-saturating prior in [0, 1).

    ``log1p(raw) / log1p(raw + damping)`` → 0 at raw 0, rising to but never
    reaching 1; ``damping`` sets how fast it saturates (larger = flatter, so big
    hits don't dominate). With ``damping <= 0`` it degrades to a hard 1.0 for any
    positive mass.
    """
    if raw <= 0.0:
        return 0.0
    if damping <= 0.0:
        return 1.0
    return math.log1p(raw) / math.log1p(raw + damping)


class PopularityModel:
    """Global per-book interest, folded from interactions (+ a feature prior)."""

    def __init__(
        self,
        *,
        half_life_days: float = 30.0,
        damping: float = 10.0,
    ) -> None:
        self._half_life_days = half_life_days
        self._damping = damping
        self._raw: dict[str, float] = {}

    @classmethod
    def from_interactions(
        cls,
        interactions: Iterable[Interaction],
        *,
        as_of: datetime,
        half_life_days: float = 30.0,
        damping: float = 10.0,
        kind_weights: dict[InteractionKind, float] | None = None,
        feature_prior: Mapping[str, BookFeatures] | None = None,
    ) -> PopularityModel:
        """Build the model from the interaction log + an optional feature prior.

        Each event adds its recency-decayed positive feedback to the book's raw
        mass (dislikes contribute nothing to *popularity* — an unpopular book is
        one nobody engaged with, not one people disliked). A book's precomputed
        ``BookFeatures.popularity`` is added as a seed prior so freshly-loaded
        corpora with a backfilled popularity still rank before any live events.
        """
        model = cls(half_life_days=half_life_days, damping=damping)
        for event in interactions:
            signal = event.signal(kind_weights)
            if signal <= 0.0:
                continue
            age_s = (as_of - event.at).total_seconds()
            model._raw[event.book_id] = model._raw.get(event.book_id, 0.0) + signal * decay_factor(
                age_s, half_life_days
            )
        if feature_prior:
            for book_id, feat in feature_prior.items():
                if feat.popularity > 0.0:
                    model._raw[book_id] = model._raw.get(book_id, 0.0) + feat.popularity
        return model

    def raw(self, book_id: str) -> float:
        """The undamped popularity mass for a book (0 if unseen)."""
        return self._raw.get(book_id, 0.0)

    def score(self, book_id: str) -> float:
        """The damped popularity prior in [0, 1) for a book."""
        return damp_popularity(self._raw.get(book_id, 0.0), damping=self._damping)

    def top(
        self, *, k: int, exclude: Iterable[str] = (), universe: Iterable[str] | None = None
    ) -> list[tuple[str, float]]:
        """The ``k`` most popular books (damped score), excluding ids.

        ``universe`` optionally restricts the candidate set (e.g. to the corpus
        the caller can actually recommend); without it, only books that appear in
        the fold are considered.
        """
        if k <= 0:
            return []
        skip = set(exclude)
        ids = list(universe) if universe is not None else list(self._raw.keys())
        scored = [(b, self.score(b)) for b in ids if b not in skip and self.score(b) > 0.0]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]


def is_cold_user(engaged_count: int, *, min_for_cf: int = 2) -> bool:
    """True when a user has too little history for collaborative filtering.

    CF needs at least a couple of engaged items to find co-occurring readers; a
    user below that threshold is "cold for CF" and should be served by content +
    popularity instead.
    """
    return engaged_count < max(1, min_for_cf)


def popularity_candidates(
    model: PopularityModel,
    *,
    k: int,
    exclude: Iterable[str] = (),
    universe: Iterable[str] | None = None,
) -> list[tuple[str, float]]:
    """Cold-start recall: the top popular books as ``(book_id, score)`` pairs."""
    return model.top(k=k, exclude=exclude, universe=universe)


__all__ = [
    "PopularityModel",
    "damp_popularity",
    "is_cold_user",
    "popularity_candidates",
]
