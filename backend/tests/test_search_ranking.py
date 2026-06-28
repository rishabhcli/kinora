"""Unit tests for ranking math: BM25 + reciprocal-rank fusion."""

from __future__ import annotations

import math

from app.search.ranking import (
    BM25,
    RankedList,
    idf,
    reciprocal_rank_fusion,
    weighted_score_fusion,
)


def test_idf_non_negative_even_for_common_terms() -> None:
    # A term in nearly every doc still yields a non-negative IDF (the +1 inside log).
    assert idf(num_docs=100, doc_freq=99) >= 0.0
    # A rare term yields a higher IDF than a common one.
    assert idf(100, 1) > idf(100, 50)


def test_bm25_tf_saturation() -> None:
    bm25 = BM25(num_docs=100, avg_doc_len=10.0)
    s1 = bm25.score_term(tf=1, doc_len=10, doc_freq=5)
    s10 = bm25.score_term(tf=10, doc_len=10, doc_freq=5)
    # 10 occurrences score more than 1, but far less than 10x (saturation).
    assert s10 > s1
    assert s10 < 10 * s1


def test_bm25_length_normalization() -> None:
    bm25 = BM25(num_docs=100, avg_doc_len=10.0)
    short = bm25.score_term(tf=2, doc_len=5, doc_freq=5)
    long = bm25.score_term(tf=2, doc_len=50, doc_freq=5)
    # Same TF in a longer document scores lower (the term is "diluted").
    assert short > long


def test_bm25_zero_tf_is_zero() -> None:
    bm25 = BM25(num_docs=10, avg_doc_len=10.0)
    assert bm25.score_term(tf=0, doc_len=10, doc_freq=3) == 0.0


def test_bm25_score_sums_query_terms() -> None:
    bm25 = BM25(num_docs=100, avg_doc_len=10.0)
    total = bm25.score(
        term_tfs={"frost": 2, "castle": 1},
        doc_len=10,
        doc_freqs={"frost": 5, "castle": 10},
    )
    expected = bm25.score_term(tf=2, doc_len=10, doc_freq=5) + bm25.score_term(
        tf=1, doc_len=10, doc_freq=10
    )
    assert math.isclose(total, expected)


def test_rrf_fuses_two_lists() -> None:
    # "b" appears in BOTH lists (a consensus item); the others appear once each,
    # so RRF's "agreement bonus" lifts "b" to the top.
    lex = RankedList(doc_ids=["x", "b", "y"])
    vec = RankedList(doc_ids=["z", "b", "w"])
    fused = reciprocal_rank_fusion([lex, vec], k=60)
    assert fused[0][0] == "b"


def test_rrf_consensus_beats_single_top() -> None:
    # A doc ranked 2nd in both lists beats a doc ranked 1st in only one list.
    lex = RankedList(doc_ids=["a", "b"])
    vec = RankedList(doc_ids=["c", "b"])
    fused = dict(reciprocal_rank_fusion([lex, vec], k=10))
    assert fused["b"] > fused["a"]
    assert fused["b"] > fused["c"]


def test_rrf_respects_weights() -> None:
    lex = RankedList(doc_ids=["a", "b"], weight=10.0)
    vec = RankedList(doc_ids=["b", "a"], weight=0.1)
    fused = dict(reciprocal_rank_fusion([lex, vec]))
    # The heavily-weighted lexical arm should dominate -> "a" first.
    assert fused["a"] > fused["b"]


def test_rrf_doc_in_one_list_only() -> None:
    fused = dict(
        reciprocal_rank_fusion(
            [RankedList(doc_ids=["a"]), RankedList(doc_ids=["b"])]
        )
    )
    assert set(fused) == {"a", "b"}


def test_weighted_score_fusion_normalizes() -> None:
    # Lexical scores on a big scale shouldn't dominate purely by units.
    lex = ({"a": 100.0, "b": 50.0}, 1.0)
    vec = ({"a": 0.1, "b": 0.9}, 1.0)
    fused = dict(weighted_score_fusion([lex, vec], normalize=True))
    # b: lexical 0.0 + vec 1.0 = 1.0; a: lexical 1.0 + vec 0.0 = 1.0 -> tie.
    assert math.isclose(fused["a"], fused["b"])
