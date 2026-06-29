"""Tests for the compressed flat index (PQ/SQ) incl. recall vs exact."""

from __future__ import annotations

import numpy as np
import pytest

from app.datascale.vectorsearch.brute_force import BruteForceIndex
from app.datascale.vectorsearch.pq_index import QuantizedFlatIndex

from .conftest import Corpus


def _recall(idx: QuantizedFlatIndex, corpus: Corpus, k: int, **skw: object) -> float:
    bf = BruteForceIndex(corpus.dim)
    bf.add_many(corpus.ids, corpus.rows())
    hit = total = 0
    for qi in range(corpus.queries.shape[0]):
        q = corpus.queries[qi]
        truth = set(bf.exact_neighbors(q, k))
        got = {r.id for r in idx.search(q, k, **skw)}  # type: ignore[arg-type]
        hit += len(truth & got)
        total += len(truth)
    return hit / total


def test_pq_with_rerank_high_recall(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="pq", m=16, nbits=8, seed=1)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids, clustered.rows(), metadatas=clustered.metadatas)
    recall = _recall(idx, clustered, 10, rerank=200)
    assert recall >= 0.95, recall


def test_sq_recall_high(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="sq", sq_bits=8, seed=1)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids, clustered.rows())
    # SQ is near-lossless so even direct (no rerank) recall is high.
    recall = _recall(idx, clustered, 10, rerank=0)
    assert recall >= 0.90, recall


def test_pq_compression_ratio(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="pq", m=12, nbits=8)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids, clustered.rows())
    # float32(48d)=192B vs 12 PQ codes=12B → 16x.
    assert abs(idx.compression_ratio() - 16.0) < 1e-6
    assert idx.memory_bytes() == clustered.n * 12


def test_requires_training_before_add() -> None:
    idx = QuantizedFlatIndex(16, kind="pq", m=4, nbits=8)
    with pytest.raises(RuntimeError):
        idx.add("a", [0.0] * 16)


def test_metadata_filter_on_quantized(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="pq", m=16, nbits=8, seed=2)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids, clustered.rows(), metadatas=clustered.metadatas)
    res = idx.search(clustered.queries[0], 10, rerank=300, where={"book": "book_3"})
    assert len(res) > 0
    assert all(r.metadata is not None and r.metadata["book"] == "book_3" for r in res)


def test_remove_from_quantized(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="sq", sq_bits=8)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids[:100], [clustered.vectors[i] for i in range(100)])
    assert idx.remove("v0") is True
    assert "v0" not in idx and len(idx) == 99
    assert idx.remove("v0") is False
    got = {r.id for r in idx.search(clustered.vectors[0], 10, rerank=0)}
    assert "v0" not in got


def test_update_in_quantized(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="sq", sq_bits=8)
    idx.train(clustered.vectors)
    idx.add("a", clustered.vectors[0])
    idx.add("a", clustered.vectors[1])  # replace
    assert len(idx) == 1


def test_no_originals_disables_rerank(clustered: Corpus) -> None:
    idx = QuantizedFlatIndex(clustered.dim, kind="pq", m=16, nbits=8, keep_originals=False, seed=3)
    idx.train(clustered.vectors)
    idx.add_many(clustered.ids[:200], [clustered.vectors[i] for i in range(200)])
    # Should still return k results (coarse only), just lower fidelity.
    res = idx.search(clustered.queries[0], 10)
    assert len(res) == 10
    assert idx.memory_bytes() == 200 * 16


def test_empty_and_zero_k() -> None:
    idx = QuantizedFlatIndex(8, kind="sq", sq_bits=8)
    rng = np.random.default_rng(0)
    idx.train(rng.standard_normal((50, 8)).astype("float32"))
    assert idx.search([0.0] * 8, 5) == []  # nothing added
    idx.add("a", [1.0] + [0.0] * 7)
    assert idx.search([1.0] + [0.0] * 7, 0) == []
