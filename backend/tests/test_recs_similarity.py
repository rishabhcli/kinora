"""Unit tests for content-based similarity recall."""

from __future__ import annotations

import math

import pytest

from app.recommendations.similarity import (
    content_candidates,
    most_similar_books,
    tag_overlap,
    weighted_centroid,
)
from app.recommendations.types import BookFeatures


def test_weighted_centroid_is_normalized_mean() -> None:
    out = weighted_centroid([[1.0, 0.0], [0.0, 1.0]])
    # Mean is (0.5, 0.5); normalized → (1/√2, 1/√2).
    assert out == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])


def test_weighted_centroid_respects_weights() -> None:
    out = weighted_centroid([[1.0, 0.0], [0.0, 1.0]], weights=[3.0, 1.0])
    # Weighted mean (0.75, 0.25) → normalized.
    norm = math.sqrt(0.75**2 + 0.25**2)
    assert out == pytest.approx([0.75 / norm, 0.25 / norm])


def test_weighted_centroid_empty_and_zero_weights() -> None:
    assert weighted_centroid([]) == []
    assert weighted_centroid([[1.0, 0.0]], weights=[0.0]) == []


def test_weighted_centroid_dim_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="dimension"):
        weighted_centroid([[1.0, 0.0], [1.0]])


def test_weighted_centroid_weight_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length"):
        weighted_centroid([[1.0, 0.0]], weights=[1.0, 2.0])


def _feat(book_id: str, vec: list[float], **kw: object) -> BookFeatures:
    return BookFeatures(book_id=book_id, embedding=vec, **kw)  # type: ignore[arg-type]


def test_most_similar_books_ranks_by_cosine_and_excludes() -> None:
    feats = [
        _feat("a", [1.0, 0.0]),
        _feat("b", [0.9, 0.1]),
        _feat("c", [0.0, 1.0]),
        _feat("noemb", []),
    ]
    out = most_similar_books([1.0, 0.0], feats, k=2, exclude={"a"})
    ids = [f.book_id for f, _ in out]
    assert ids == ["b", "c"]  # 'a' excluded; b closer than c; noemb never matches


def test_most_similar_books_min_score_filter() -> None:
    feats = [_feat("a", [1.0, 0.0]), _feat("c", [0.0, 1.0])]
    out = most_similar_books([1.0, 0.0], feats, k=5, min_score=0.5)
    assert [f.book_id for f, _ in out] == ["a"]  # c has cosine 0 < 0.5


def test_content_candidates_attributes_best_seed() -> None:
    features = {
        "seed_fantasy": _feat("seed_fantasy", [1.0, 0.0, 0.0]),
        "seed_scifi": _feat("seed_scifi", [0.0, 1.0, 0.0]),
        "cand_fantasy": _feat("cand_fantasy", [0.95, 0.05, 0.0]),
        "cand_scifi": _feat("cand_scifi", [0.0, 0.95, 0.05]),
    }
    seeds = {"seed_fantasy": 4.0, "seed_scifi": 4.0}
    out = content_candidates(seeds, features, k=5)
    by_id = {bid: (sim, seed) for bid, sim, seed in out}
    # Each candidate attributed to its own-genre seed.
    assert by_id["cand_fantasy"][1] == "seed_fantasy"
    assert by_id["cand_scifi"][1] == "seed_scifi"
    # Seeds themselves are never recommended back.
    assert "seed_fantasy" not in by_id


def test_content_candidates_empty_when_no_engaged_seeds() -> None:
    features = {"a": _feat("a", [1.0])}
    assert content_candidates({}, features, k=5) == []
    # A seed with no embedding yields nothing.
    assert content_candidates({"x": 1.0}, {"x": _feat("x", [])}, k=5) == []


def test_tag_overlap_jaccard() -> None:
    a = BookFeatures("a", tags=("fantasy", "epic"))
    b = BookFeatures("b", tags=("fantasy", "romance"))
    assert tag_overlap(a, b) == pytest.approx(1 / 3)
    assert tag_overlap(BookFeatures("a"), BookFeatures("b")) == 0.0
