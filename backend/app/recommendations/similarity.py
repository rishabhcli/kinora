"""Content-based similarity over book embeddings (the §8 shared 1152-d space).

The content signal answers "more books *like* the ones you engaged with". Every
book carries a content embedding (its canon centroid; see
:class:`~app.recommendations.types.BookFeatures`), so similarity is cosine in the
same shared image+text space the memory layer uses — we reuse
``app.memory.retrieval.cosine`` / ``normalize`` rather than re-deriving the math.

Two recall strategies live here:

* :func:`most_similar_books` — k-NN by cosine against a single query vector.
* :func:`content_candidates` — the user-facing recall: build a *profile* vector
  from the user's recently-engaged books (a weighted centroid) and k-NN against
  it, attributing each hit to the strongest seed book ("because you read X").

Pure and offline — operates on already-computed feature vectors. The DB ANN
index is the coarse recall; these are the fine pass over its candidates.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from app.memory.retrieval import cosine, normalize

from .types import BookFeatures, Vector


def weighted_centroid(vectors: Sequence[Vector], weights: Sequence[float] | None = None) -> Vector:
    """The (optionally weighted) mean of ``vectors``, L2-normalized.

    The taste/profile vector of a set of books is their centroid in the shared
    space; normalizing keeps cosine comparisons against it on the same footing as
    any other unit vector. Zero/empty inputs return ``[]`` (no signal).

    Raises:
        ValueError: if ``weights`` is given with a different length, or the
            vectors are not all the same non-zero dimension.
    """
    vecs = [list(v) for v in vectors if v]
    if not vecs:
        return []
    dim = len(vecs[0])
    if dim == 0 or any(len(v) != dim for v in vecs):
        raise ValueError("all vectors must share one non-zero dimension")
    if weights is None:
        ws = [1.0] * len(vecs)
    else:
        ws = [float(w) for w in weights]
        if len(ws) != len(vectors):
            raise ValueError("weights length must match vectors length")
        # Drop weights aligned to dropped (empty) vectors by re-pairing.
        ws = [w for w, v in zip(ws, vectors, strict=True) if v]
    total = math.fsum(ws)
    if total == 0.0:
        return []
    acc = [0.0] * dim
    for vec, w in zip(vecs, ws, strict=True):
        for i, x in enumerate(vec):
            acc[i] += w * x
    centroid = [a / total for a in acc]
    return normalize(centroid)


def most_similar_books(
    query: Vector,
    features: Iterable[BookFeatures],
    *,
    k: int,
    exclude: Iterable[str] = (),
    min_score: float = 0.0,
) -> list[tuple[BookFeatures, float]]:
    """Top-``k`` books by cosine to ``query`` (excluding ids, above ``min_score``).

    Returns ``(book, similarity)`` pairs sorted by descending similarity. Books
    without a content embedding never match (cosine against them is 0).
    """
    if not query or k <= 0:
        return []
    skip = set(exclude)
    scored: list[tuple[BookFeatures, float]] = []
    for feat in features:
        if feat.book_id in skip or not feat.has_embedding:
            continue
        sim = cosine(query, feat.embedding)
        if sim >= min_score:
            scored.append((feat, sim))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


def content_candidates(
    seed_books: Mapping[str, float],
    features: Mapping[str, BookFeatures],
    *,
    k: int,
    min_score: float = 0.0,
    exclude: Iterable[str] = (),
) -> list[tuple[str, float, str]]:
    """Content recall from a user's engaged books → ``(book_id, sim, seed_id)``.

    ``seed_books`` maps each engaged ``book_id`` to its (positive) engagement
    weight. For each candidate book *not* already engaged, its content score is
    the **best** weighted cosine to any seed (so the hit is attributed to the
    single most-responsible "because you read X" seed), and the returned tuple
    carries that seed id. This per-seed attribution is what makes the content
    reason explainable rather than an opaque centroid match.

    ``exclude`` drops books the user has already interacted with even when they
    aren't positive *seeds* — e.g. a disliked book (net-negative, so absent from
    ``seed_books``) must never be recommended back.
    """
    if not seed_books or k <= 0:
        return []
    seed_feats = {
        bid: features[bid]
        for bid, w in seed_books.items()
        if bid in features and features[bid].has_embedding and w > 0.0
    }
    if not seed_feats:
        return []
    skip = set(seed_books) | set(exclude)
    out: list[tuple[str, float, str]] = []
    for book_id, feat in features.items():
        if book_id in skip or not feat.has_embedding:
            continue
        best_sim = float("-inf")
        best_seed = ""
        for seed_id, seed_feat in seed_feats.items():
            sim = cosine(feat.embedding, seed_feat.embedding) * _seed_gain(seed_books[seed_id])
            if sim > best_sim:
                best_sim = sim
                best_seed = seed_id
        if best_sim >= min_score:
            out.append((book_id, best_sim, best_seed))
    out.sort(key=lambda t: t[1], reverse=True)
    return out[:k]


def _seed_gain(weight: float) -> float:
    """Diminishing-returns gain on a seed's engagement weight (saturating in [0,1)).

    A book engaged with 10× shouldn't dominate one engaged with 5× by 2×; we
    pass the weight through ``w / (1 + w)`` so strong seeds count more but with
    diminishing returns, keeping content recall from collapsing onto one book.
    """
    if weight <= 0.0:
        return 0.0
    return weight / (1.0 + weight)


def tag_overlap(a: BookFeatures, b: BookFeatures) -> float:
    """Jaccard overlap of two books' tag sets in [0, 1] (the sparse content side)."""
    ta, tb = set(a.tags), set(b.tags)
    if not ta and not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


__all__ = [
    "content_candidates",
    "most_similar_books",
    "tag_overlap",
    "weighted_centroid",
]
