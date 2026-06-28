"""Collaborative filtering — item-item and user-item neighbourhood models.

CF answers "readers *like you* also read X" with no content knowledge at all —
purely from the structure of the interaction log. Two classic neighbourhood
models are built over the same sparse user×item matrix of implicit-feedback
weights:

* **Item-item CF** (:class:`ItemItemCF`) — precompute book↔book similarity as the
  cosine between their *user columns* (two books are similar if the same readers
  engaged with both). To recommend for a user, score each candidate book by the
  sum of its similarities to the books that user already engaged with, weighted
  by the user's engagement. This is the workhorse: stable, explainable (the
  most-similar engaged book is the "because you read X" seed), and cheap to serve
  once the item-item matrix is built.

* **User-item CF** (:class:`UserUserCF`) — find the user's nearest *neighbour
  readers* (cosine between their rating rows) and recommend what those neighbours
  engaged with that the target user hasn't. Complements item-item on sparse tails.

Everything is a pure fold over a list of :class:`~.types.Interaction` — no DB, no
numpy; the matrices are dicts keyed by id, sized to the corpus the service hands
in (already scoped to a book/user neighbourhood by the DB recall). Tests pin the
cosines against hand-computed co-occurrence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .types import Interaction, InteractionKind


@dataclass(slots=True)
class InteractionMatrix:
    """A sparse user×item matrix of net implicit-feedback weights.

    ``user_items[user][book]`` and the transposed ``item_users[book][user]`` hold
    the *net* signed weight (so repeated views accumulate and a later dislike can
    net out earlier positives). Only positive net cells are kept as "engaged" for
    recall, but the raw signed value drives the cosine so a dislike still shapes
    similarity. Built once per request batch and shared by both CF models.
    """

    user_items: dict[str, dict[str, float]] = field(default_factory=dict)
    item_users: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def from_interactions(
        cls,
        interactions: Iterable[Interaction],
        *,
        kind_weights: dict[InteractionKind, float] | None = None,
    ) -> InteractionMatrix:
        """Fold an interaction stream into the sparse matrix (net weight per cell)."""
        matrix = cls()
        for event in interactions:
            w = event.signal(kind_weights)
            row = matrix.user_items.setdefault(event.user_id, {})
            row[event.book_id] = row.get(event.book_id, 0.0) + w
            col = matrix.item_users.setdefault(event.book_id, {})
            col[event.user_id] = col.get(event.user_id, 0.0) + w
        return matrix

    @property
    def users(self) -> list[str]:
        return list(self.user_items.keys())

    @property
    def items(self) -> list[str]:
        return list(self.item_users.keys())

    def engaged_items(self, user_id: str) -> dict[str, float]:
        """The books a user net-positively engaged with → weight (the recall seeds)."""
        return {b: w for b, w in self.user_items.get(user_id, {}).items() if w > 0.0}


def _sparse_cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Cosine between two sparse vectors (dicts); 0 if either is empty/zero."""
    if not a or not b:
        return 0.0
    # Iterate the smaller dict for the dot product.
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = math.fsum(v * large[k] for k, v in small.items() if k in large)
    na = math.sqrt(math.fsum(v * v for v in a.values()))
    nb = math.sqrt(math.fsum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True, slots=True)
class CFHit:
    """One CF-recommended book: its score and the strongest contributing seed."""

    book_id: str
    score: float
    seed_book_id: str | None = None
    seed_weight: float = 0.0


class ItemItemCF:
    """Item-item collaborative filtering over an :class:`InteractionMatrix`."""

    def __init__(
        self,
        matrix: InteractionMatrix,
        *,
        min_cooccur: int = 1,
        neighbors: int = 40,
    ) -> None:
        self._matrix = matrix
        self._min_cooccur = max(1, min_cooccur)
        self._neighbors = max(1, neighbors)
        self._sim_cache: dict[str, dict[str, float]] = {}

    def similar_items(self, book_id: str) -> dict[str, float]:
        """Books most similar to ``book_id`` by shared-reader cosine (cached).

        Two books are similar when the same readers engaged with both; the
        ``min_cooccur`` floor suppresses spurious one-reader edges that would
        otherwise read as cosine 1.0 on a single shared reader.
        """
        if book_id in self._sim_cache:
            return self._sim_cache[book_id]
        col = self._matrix.item_users.get(book_id, {})
        sims: list[tuple[str, float]] = []
        if col:
            readers = set(col)
            for other, other_col in self._matrix.item_users.items():
                if other == book_id:
                    continue
                if len(readers & set(other_col)) < self._min_cooccur:
                    continue
                sim = _sparse_cosine(col, other_col)
                if sim > 0.0:
                    sims.append((other, sim))
        sims.sort(key=lambda t: t[1], reverse=True)
        top = dict(sims[: self._neighbors])
        self._sim_cache[book_id] = top
        return top

    def recommend(self, user_id: str, *, k: int, exclude: Iterable[str] = ()) -> list[CFHit]:
        """Score candidate books for ``user_id`` by item-item neighbourhood.

        Candidate score = Σ over the user's engaged books of
        ``engagement_weight · item_similarity``. Each candidate's "because you
        read X" seed is the single engaged book contributing the most.
        """
        seeds = self._matrix.engaged_items(user_id)
        if not seeds or k <= 0:
            return []
        skip = set(exclude) | set(self._matrix.user_items.get(user_id, {}))
        scores: dict[str, float] = {}
        best_seed: dict[str, tuple[str, float]] = {}
        for seed_book, seed_w in seeds.items():
            for cand, sim in self.similar_items(seed_book).items():
                if cand in skip:
                    continue
                contrib = seed_w * sim
                scores[cand] = scores.get(cand, 0.0) + contrib
                if cand not in best_seed or contrib > best_seed[cand][1]:
                    best_seed[cand] = (seed_book, contrib)
        hits = [
            CFHit(
                book_id=b,
                score=s,
                seed_book_id=best_seed[b][0],
                seed_weight=best_seed[b][1],
            )
            for b, s in scores.items()
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


class UserUserCF:
    """User-item collaborative filtering: recommend via nearest-neighbour readers."""

    def __init__(self, matrix: InteractionMatrix, *, neighbors: int = 40) -> None:
        self._matrix = matrix
        self._neighbors = max(1, neighbors)

    def neighbours(self, user_id: str) -> list[tuple[str, float]]:
        """The user's nearest reader-neighbours by rating-row cosine, descending."""
        me = self._matrix.user_items.get(user_id, {})
        if not me:
            return []
        sims: list[tuple[str, float]] = []
        for other, row in self._matrix.user_items.items():
            if other == user_id:
                continue
            sim = _sparse_cosine(me, row)
            if sim > 0.0:
                sims.append((other, sim))
        sims.sort(key=lambda t: t[1], reverse=True)
        return sims[: self._neighbors]

    def recommend(self, user_id: str, *, k: int, exclude: Iterable[str] = ()) -> list[CFHit]:
        """Score books by what the user's nearest neighbours engaged with.

        Candidate score = Σ over neighbours of
        ``neighbour_similarity · neighbour_engagement_weight``, over books the
        target user hasn't engaged with. The seed is the most-similar neighbour's
        own strongest engaged book (a readers-like-you provenance).
        """
        if k <= 0:
            return []
        skip = set(exclude) | set(self._matrix.user_items.get(user_id, {}))
        scores: dict[str, float] = {}
        best_seed: dict[str, tuple[str, float]] = {}
        for neighbour, nsim in self.neighbours(user_id):
            for book, w in self._matrix.engaged_items(neighbour).items():
                if book in skip:
                    continue
                contrib = nsim * w
                scores[book] = scores.get(book, 0.0) + contrib
                if book not in best_seed or contrib > best_seed[book][1]:
                    best_seed[book] = (neighbour, contrib)
        hits = [
            CFHit(book_id=b, score=s, seed_book_id=best_seed[b][0], seed_weight=best_seed[b][1])
            for b, s in scores.items()
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


__all__ = [
    "CFHit",
    "InteractionMatrix",
    "ItemItemCF",
    "UserUserCF",
]
