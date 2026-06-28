"""The per-user taste vector — a recency-decayed summary of a reader's taste.

A taste vector is a single dense vector in the shared 1152-d space (§8) that
points "where the reader's interest lives". It is a weighted centroid of the
content embeddings of the books they engaged with, where each book's weight is::

    weight = implicit_feedback(kind) · decay(age)

* **Implicit feedback** turns the event kind into a signed strength (a *finish*
  or *like* counts more than a *view*; a *dislike* is negative — it pushes the
  vector *away* from that book). See :data:`~.types.DEFAULT_KIND_WEIGHTS`.
* **Recency decay** is exponential with a configurable half-life: a book read
  today counts fully, one read a half-life ago counts half. This is the recsys's
  "timely forgetting" (cf. §8.5): stale taste fades without being deleted.

The model is **incremental**: :meth:`TasteModel.fold` updates an existing
accumulator from a new batch of events relative to a reference time, so the
service never re-reads a user's whole history per request — it carries the
decayed accumulator forward (the same trick the canon layer uses to keep cost
flat as histories grow). All math is pure; tests pin the decay against
hand-computed half-life values.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.memory.retrieval import cosine, normalize

from .types import (
    BookFeatures,
    Interaction,
    InteractionKind,
    Vector,
)

#: One day in seconds — the unit the half-life is expressed in.
_DAY_S = 86_400.0


def decay_factor(age_s: float, half_life_days: float) -> float:
    """Exponential recency weight in (0, 1]: ``0.5 ** (age / half_life)``.

    ``age_s`` is the event's age in seconds relative to the reference time;
    ``half_life_days`` is the decay half-life. Age 0 → 1.0; age == half-life →
    0.5. Negative ages (events "after" the reference) clamp to age 0 (weight 1).
    """
    if half_life_days <= 0.0:
        return 1.0
    age = max(0.0, age_s)
    half_life_s = half_life_days * _DAY_S
    return math.pow(0.5, age / half_life_s)


@dataclass(slots=True)
class TasteAccumulator:
    """The incremental state behind a user's taste vector.

    Holds the (un-normalized) decayed weighted sum of book embeddings plus the
    bookkeeping needed to roll the decay forward without re-reading history:

    * ``sum_vec`` — Σ ``weight_i · embedding_i`` (decayed to ``as_of``).
    * ``weight_total`` — Σ ``|weight_i|`` (the L1 mass; used to know when taste
      is "established" vs cold).
    * ``as_of`` — the timestamp the accumulator's decays are referenced to.
    * ``event_count`` — number of folded events (telemetry / cold-start gate).
    """

    sum_vec: Vector = field(default_factory=list)
    weight_total: float = 0.0
    as_of: datetime | None = None
    event_count: int = 0

    @property
    def is_cold(self) -> bool:
        """True when no positive taste mass has accumulated (cold-start gate)."""
        return self.weight_total <= 0.0 or not any(x != 0.0 for x in self.sum_vec)

    def vector(self) -> Vector:
        """The normalized taste vector (``[]`` while cold)."""
        if self.is_cold:
            return []
        return normalize(self.sum_vec)


class TasteModel:
    """Builds and folds recency-decayed per-user taste accumulators."""

    def __init__(self, *, half_life_days: float = 30.0) -> None:
        if half_life_days <= 0.0:
            raise ValueError("half_life_days must be positive")
        self._half_life_days = half_life_days

    # ---- batch build (cold start of the accumulator) -------------------- #

    def build(
        self,
        interactions: Iterable[Interaction],
        features: Mapping[str, BookFeatures],
        *,
        as_of: datetime,
        kind_weights: dict[InteractionKind, float] | None = None,
    ) -> TasteAccumulator:
        """Build a taste accumulator from scratch, decayed to ``as_of``.

        Books with no content embedding contribute nothing (no vector to add).
        Dislikes subtract, pulling the taste vector away from disliked content.
        """
        acc = TasteAccumulator(as_of=as_of)
        return self.fold(acc, interactions, features, as_of=as_of, kind_weights=kind_weights)

    # ---- incremental fold (roll the decay forward) ---------------------- #

    def fold(
        self,
        acc: TasteAccumulator,
        interactions: Iterable[Interaction],
        features: Mapping[str, BookFeatures],
        *,
        as_of: datetime,
        kind_weights: dict[InteractionKind, float] | None = None,
    ) -> TasteAccumulator:
        """Fold a new batch into ``acc``, re-referencing decays to ``as_of``.

        If ``acc`` already references an earlier time, its existing mass is first
        decayed forward to ``as_of`` (so a request "now" sees a correctly faded
        history), then the new events are added at their own ages. Returns a new
        accumulator (``acc`` is not mutated) so callers can keep the prior state.
        """
        events = list(interactions)
        dim = self._infer_dim(acc, features)
        sum_vec = list(acc.sum_vec) if acc.sum_vec else ([0.0] * dim if dim else [])
        weight_total = acc.weight_total
        count = acc.event_count

        # Decay the carried-forward mass to the new reference time.
        if acc.as_of is not None and acc.sum_vec:
            roll = decay_factor((as_of - acc.as_of).total_seconds(), self._half_life_days)
            sum_vec = [x * roll for x in sum_vec]
            weight_total *= roll

        for event in events:
            feat = features.get(event.book_id)
            if feat is None or not feat.has_embedding:
                count += 1
                continue
            if not sum_vec:
                sum_vec = [0.0] * len(feat.embedding)
            age_s = (as_of - event.at).total_seconds()
            decayed = event.signal(kind_weights) * decay_factor(age_s, self._half_life_days)
            for i, x in enumerate(feat.embedding):
                sum_vec[i] += decayed * x
            weight_total += abs(decayed)
            count += 1

        return TasteAccumulator(
            sum_vec=sum_vec,
            weight_total=weight_total,
            as_of=as_of,
            event_count=count,
        )

    @staticmethod
    def _infer_dim(acc: TasteAccumulator, features: Mapping[str, BookFeatures]) -> int:
        if acc.sum_vec:
            return len(acc.sum_vec)
        for feat in features.values():
            if feat.has_embedding:
                return len(feat.embedding)
        return 0


def taste_similarity(taste: Vector, book: BookFeatures) -> float:
    """Cosine of a taste vector against a book's content embedding (0 if cold)."""
    if not taste or not book.has_embedding:
        return 0.0
    return cosine(taste, book.embedding)


def engaged_book_weights(
    interactions: Sequence[Interaction],
    *,
    as_of: datetime,
    half_life_days: float,
    kind_weights: dict[InteractionKind, float] | None = None,
) -> dict[str, float]:
    """Collapse a user's events into per-book net decayed engagement weights.

    The content-recall seed map (see ``similarity.content_candidates``): each
    book the user touched mapped to its summed ``feedback · decay`` weight. Books
    whose net weight is non-positive (dominated by dislikes) are dropped — they
    are not "engaged" seeds to find more of.
    """
    weights: dict[str, float] = {}
    for event in interactions:
        age_s = (as_of - event.at).total_seconds()
        w = event.signal(kind_weights) * decay_factor(age_s, half_life_days)
        weights[event.book_id] = weights.get(event.book_id, 0.0) + w
    return {bid: w for bid, w in weights.items() if w > 0.0}


__all__ = [
    "TasteAccumulator",
    "TasteModel",
    "decay_factor",
    "engaged_book_weights",
    "taste_similarity",
]
