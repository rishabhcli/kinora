"""Tests for metadata predicates, the BM25 keyword index and score fusion."""

from __future__ import annotations

import pytest

from app.datascale.vectorsearch.filtering import (
    Bm25KeywordIndex,
    Predicate,
    cosine_to_unit,
    fuse_scores,
    tokenize,
)

# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #


def test_bare_equality() -> None:
    p = Predicate.coerce({"book": "b1", "page": 3})
    assert p is not None
    assert p.matches({"book": "b1", "page": 3})
    assert not p.matches({"book": "b1", "page": 4})
    assert not p.matches({"book": "b1"})  # missing page


def test_comparison_operators() -> None:
    p = Predicate.coerce({"page": {"$gte": 5, "$lt": 10}})
    assert p is not None
    assert p.matches({"page": 5})
    assert p.matches({"page": 9})
    assert not p.matches({"page": 10})
    assert not p.matches({"page": 4})


def test_in_nin_operators() -> None:
    p = Predicate.coerce({"book": {"$in": ["a", "b"]}})
    assert p is not None and p.matches({"book": "a"}) and not p.matches({"book": "c"})
    p2 = Predicate.coerce({"book": {"$nin": ["a", "b"]}})
    assert p2 is not None and p2.matches({"book": "c"}) and not p2.matches({"book": "a"})


def test_exists_operator() -> None:
    p = Predicate.coerce({"voice": {"$exists": True}})
    assert p is not None
    assert p.matches({"voice": "x"})
    assert not p.matches({"book": "b"})
    p2 = Predicate.coerce({"voice": {"$exists": False}})
    assert p2 is not None and p2.matches({"book": "b"}) and not p2.matches({"voice": "x"})


def test_contains_operator() -> None:
    p = Predicate.coerce({"tags": {"$contains": "hero"}})
    assert p is not None
    assert p.matches({"tags": ["hero", "sword"]})
    assert not p.matches({"tags": ["villain"]})
    p2 = Predicate.coerce({"text": {"$contains": "drag"}})
    assert p2 is not None and p2.matches({"text": "the dragon flies"})


def test_boolean_combinators() -> None:
    p = Predicate.coerce({"$or": [{"book": "a"}, {"page": {"$gt": 100}}]})
    assert p is not None
    assert p.matches({"book": "a", "page": 1})
    assert p.matches({"book": "z", "page": 200})
    assert not p.matches({"book": "z", "page": 5})

    p2 = Predicate.coerce({"$and": [{"book": "a"}, {"$not": {"page": 1}}]})
    assert p2 is not None
    assert p2.matches({"book": "a", "page": 2})
    assert not p2.matches({"book": "a", "page": 1})


def test_none_metadata_is_empty() -> None:
    p = Predicate.coerce({"book": "a"})
    assert p is not None and not p.matches(None)


def test_coerce_none_and_bad_type() -> None:
    assert Predicate.coerce(None) is None
    existing = Predicate({"a": 1})
    assert Predicate.coerce(existing) is existing
    with pytest.raises(TypeError):
        Predicate.coerce(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_unknown_operator_raises() -> None:
    p = Predicate.coerce({"page": {"$weird": 1}})
    assert p is not None
    with pytest.raises(ValueError):
        p.matches({"page": 1})


# --------------------------------------------------------------------------- #
# Tokenize + BM25
# --------------------------------------------------------------------------- #


def test_tokenize() -> None:
    assert tokenize("The Snow-Queen, v2!") == ["the", "snow", "queen", "v2"]


def test_bm25_ranks_relevant_docs_higher() -> None:
    bm = Bm25KeywordIndex()
    bm.add_text("d1", "the snow queen rules the ice palace")
    bm.add_text("d2", "a warm summer beach holiday")
    bm.add_text("d3", "snow falls on the palace gates")
    scores = bm.score(tokenize("snow palace"))
    # d1 has both terms, d3 has both, d2 has neither (absent from scores).
    assert "d2" not in scores
    assert scores["d1"] > 0 and scores["d3"] > 0


def test_bm25_normalised_in_unit_range() -> None:
    bm = Bm25KeywordIndex()
    bm.add_text("d1", "alpha beta gamma")
    bm.add_text("d2", "beta beta beta")
    norm = bm.score_normalised(tokenize("beta"))
    assert all(0.0 <= v <= 1.0 for v in norm.values())
    assert max(norm.values()) == pytest.approx(1.0)


def test_bm25_remove_and_replace() -> None:
    bm = Bm25KeywordIndex()
    bm.add_text("d1", "hello world")
    assert bm.n_docs == 1
    bm.add_text("d1", "different words now")  # replace
    assert bm.n_docs == 1
    assert bm.score(tokenize("hello")) == {}
    assert bm.remove("d1") is True
    assert bm.n_docs == 0
    assert bm.remove("d1") is False


def test_bm25_empty_index() -> None:
    bm = Bm25KeywordIndex()
    assert bm.score(["anything"]) == {}
    assert bm.score_normalised(["anything"]) == {}


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #


def test_cosine_to_unit() -> None:
    assert cosine_to_unit(1.0) == pytest.approx(1.0)
    assert cosine_to_unit(-1.0) == pytest.approx(0.0)
    assert cosine_to_unit(0.0) == pytest.approx(0.5)


def test_fuse_scores_union_and_weighting() -> None:
    dense = {"a": 0.8, "b": 0.4}
    lexical = {"b": 1.0, "c": 0.5}
    fused = fuse_scores(dense, lexical, alpha=0.5)
    assert fused["a"] == pytest.approx(0.4)  # 0.5*0.8 + 0
    assert fused["b"] == pytest.approx(0.7)  # 0.5*0.4 + 0.5*1.0
    assert fused["c"] == pytest.approx(0.25)  # 0 + 0.5*0.5


def test_fuse_scores_alpha_bounds() -> None:
    with pytest.raises(ValueError):
        fuse_scores({}, {}, alpha=1.5)
