"""Deterministic synthetic corpus + interaction-log generator (for eval/tests).

The offline eval harness needs *labelled* data: a book corpus with known latent
structure (clusters of similar books) and a population of users whose
interactions follow a latent taste, so a good recommender can be measured
against ground truth. This module builds exactly that, **seeded** so every run is
reproducible and **fake-embedding-only** (one-hot-ish cluster vectors) so it
spends zero credits and pins cleanly in tests.

The generative model:

* ``num_clusters`` latent topics; each book belongs to one cluster and gets a
  cluster-centroid embedding plus a small per-book jitter, so books in a cluster
  are near each other in the shared space (content similarity has signal).
* Each user is assigned a primary cluster (and a weaker secondary), then samples
  interactions biased toward books in their clusters — so users who share a
  cluster co-engage (collaborative signal has structure too).
* Book popularity follows a power-law-ish skew so cold-start has a meaningful
  head/tail.

The result is a :class:`SyntheticDataset` carrying the corpus, the full
interaction log, and — crucially — each user's *held-out* relevant books (the
ground truth the metrics score against) and their latent cluster (for coverage
/ diversity analysis).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .types import BookFeatures, Interaction, InteractionKind

#: The fake embedding dimension for synthetic books (small, so tests are fast;
#: independent of the production 1152-d — the math doesn't care about the size).
SYNTH_DIM = 32

_GENRES = (
    "fantasy",
    "scifi",
    "romance",
    "mystery",
    "history",
    "horror",
    "literary",
    "adventure",
)

_POSITIVE_KINDS = (
    InteractionKind.OPEN,
    InteractionKind.READ,
    InteractionKind.FINISH,
    InteractionKind.LIKE,
    InteractionKind.BOOKMARK,
)


@dataclass(slots=True)
class SyntheticUser:
    """A synthetic reader: their id, latent clusters, and held-out relevant books."""

    user_id: str
    primary_cluster: int
    secondary_cluster: int
    #: book_ids the user "truly" likes that were held OUT of their training log.
    held_out: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SyntheticDataset:
    """A reproducible labelled recsys dataset for the offline eval harness."""

    features: dict[str, BookFeatures]
    interactions: list[Interaction]
    users: list[SyntheticUser]
    book_cluster: dict[str, int]
    num_clusters: int
    as_of: datetime

    @property
    def held_out(self) -> dict[str, set[str]]:
        """user_id → held-out relevant book ids (the ground-truth labels)."""
        return {u.user_id: set(u.held_out) for u in self.users}

    def training_interactions(self) -> list[Interaction]:
        """The interaction log with held-out positives removed (the eval split)."""
        held = self.held_out
        return [e for e in self.interactions if e.book_id not in held.get(e.user_id, set())]


def _cluster_centroid(cluster: int, num_clusters: int, dim: int) -> list[float]:
    """A deterministic, near-orthogonal centroid for a latent cluster."""
    vec = [0.0] * dim
    # Spread each cluster's mass over a few dims keyed by the cluster id so
    # clusters are distinct but not perfectly one-hot (gives content some nuance).
    primary = cluster % dim
    secondary = (cluster * 7 + 3) % dim
    vec[primary] = 1.0
    vec[secondary] = 0.5
    return vec


def make_dataset(
    *,
    seed: int = 7,
    num_books: int = 120,
    num_users: int = 60,
    num_clusters: int = 8,
    min_events_per_user: int = 6,
    max_events_per_user: int = 20,
    held_out_per_user: int = 3,
    dim: int = SYNTH_DIM,
    as_of: datetime | None = None,
) -> SyntheticDataset:
    """Generate a reproducible labelled dataset (seeded; no network, no credits).

    The held-out positives are drawn from the user's *primary* cluster so a
    recommender that recovers the user's taste should surface them — that's what
    precision@k / recall@k / nDCG measure. Training interactions exclude those
    held-out books (see :meth:`SyntheticDataset.training_interactions`).
    """
    rng = random.Random(seed)
    now = as_of or datetime(2026, 6, 1, 12, 0, 0)

    # ---- Books -------------------------------------------------------- #
    features: dict[str, BookFeatures] = {}
    book_cluster: dict[str, int] = {}
    books_by_cluster: dict[int, list[str]] = {c: [] for c in range(num_clusters)}
    for i in range(num_books):
        cluster = i % num_clusters
        book_id = f"bk_{i:04d}"
        centroid = _cluster_centroid(cluster, num_clusters, dim)
        embedding = [max(0.0, x + rng.gauss(0.0, 0.05)) for x in centroid]
        genre = _GENRES[cluster % len(_GENRES)]
        # Power-law-ish popularity: a few hits, a long tail.
        popularity = round(max(0.0, rng.paretovariate(1.5) - 1.0) * 5.0, 3)
        features[book_id] = BookFeatures(
            book_id=book_id,
            title=f"Book {i}",
            author=f"Author {i % 25}",
            embedding=embedding,
            tags=(genre,),
            popularity=popularity,
        )
        book_cluster[book_id] = cluster
        books_by_cluster[cluster].append(book_id)

    # ---- Users + interactions ----------------------------------------- #
    users: list[SyntheticUser] = []
    interactions: list[Interaction] = []
    for u in range(num_users):
        user_id = f"u_{u:04d}"
        primary = rng.randrange(num_clusters)
        secondary = (primary + 1 + rng.randrange(max(1, num_clusters - 1))) % num_clusters
        n_events = rng.randint(min_events_per_user, max_events_per_user)

        # 80% of events from the primary cluster, 20% from the secondary.
        primary_pool = list(books_by_cluster[primary])
        secondary_pool = list(books_by_cluster[secondary])
        rng.shuffle(primary_pool)
        rng.shuffle(secondary_pool)

        touched: list[str] = []
        for _ in range(n_events):
            pool = primary_pool if rng.random() < 0.8 else secondary_pool
            if not pool:
                pool = primary_pool or secondary_pool
            if not pool:
                continue
            book_id = pool[rng.randrange(len(pool))]
            kind = _POSITIVE_KINDS[rng.randrange(len(_POSITIVE_KINDS))]
            age_days = rng.uniform(0.0, 90.0)
            interactions.append(
                Interaction(
                    user_id=user_id,
                    book_id=book_id,
                    kind=kind,
                    at=now - timedelta(days=age_days),
                )
            )
            touched.append(book_id)

        # Held-out: primary-cluster books the user did NOT touch (true positives).
        candidates = [b for b in books_by_cluster[primary] if b not in set(touched)]
        rng.shuffle(candidates)
        held = set(candidates[:held_out_per_user])
        # Also log the held-out positives (they are real likes), to be split out.
        for book_id in held:
            interactions.append(
                Interaction(
                    user_id=user_id,
                    book_id=book_id,
                    kind=InteractionKind.LIKE,
                    at=now - timedelta(days=rng.uniform(0.0, 30.0)),
                )
            )
        users.append(
            SyntheticUser(
                user_id=user_id,
                primary_cluster=primary,
                secondary_cluster=secondary,
                held_out=held,
            )
        )

    return SyntheticDataset(
        features=features,
        interactions=interactions,
        users=users,
        book_cluster=book_cluster,
        num_clusters=num_clusters,
        as_of=now,
    )


def cluster_self_similarity(dataset: SyntheticDataset) -> float:
    """Mean within-cluster vs cross-cluster embedding gap — a dataset-quality check.

    A positive value means the synthetic clusters are genuinely separable in the
    embedding space (so content similarity has signal to recover); used in tests
    to assert the generator built a learnable structure.
    """
    from app.memory.retrieval import cosine

    by_cluster: dict[int, list[list[float]]] = {}
    for book_id, cluster in dataset.book_cluster.items():
        by_cluster.setdefault(cluster, []).append(dataset.features[book_id].embedding)
    within: list[float] = []
    across: list[float] = []
    clusters = sorted(by_cluster)
    for c in clusters:
        vecs = by_cluster[c]
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                within.append(cosine(vecs[i], vecs[j]))
        for other in clusters:
            if other <= c:
                continue
            across.append(cosine(vecs[0], by_cluster[other][0]))
    w = math.fsum(within) / len(within) if within else 0.0
    a = math.fsum(across) / len(across) if across else 0.0
    return w - a


__all__ = [
    "SYNTH_DIM",
    "SyntheticDataset",
    "SyntheticUser",
    "cluster_self_similarity",
    "make_dataset",
]
