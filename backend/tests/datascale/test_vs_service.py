"""Tests for the VectorSearchService facade — backends, hybrid query, config."""

from __future__ import annotations

import pytest

from app.datascale.vectorsearch.config import (
    DEFAULT_EMBED_DIM,
    VectorSearchConfig,
)
from app.datascale.vectorsearch.service import VectorSearchService
from app.datascale.vectorsearch.types import Metric, Query

from .conftest import Corpus

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults_match_embedding_contract() -> None:
    c = VectorSearchConfig()
    assert c.dim == DEFAULT_EMBED_DIM == 1152
    assert c.metric is Metric.COSINE


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        VectorSearchConfig(dim=0)
    with pytest.raises(ValueError):
        VectorSearchConfig(backend="faiss")
    with pytest.raises(ValueError):
        VectorSearchConfig(default_alpha=2.0)


def test_config_from_mapping() -> None:
    c = VectorSearchConfig.from_mapping(
        {"embed_dim": 64, "metric": "l2", "backend": "sharded", "n_shards": 8}
    )
    assert c.dim == 64 and c.metric is Metric.L2 and c.n_shards == 8


def test_config_from_object() -> None:
    class S:
        embed_dim = 32
        metric = "dot"

    c = VectorSearchConfig.from_mapping(S())
    assert c.dim == 32 and c.metric is Metric.DOT


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", ["hnsw", "sharded", "brute"])
def test_backends_basic_search(backend: str, small_clustered: Corpus) -> None:
    svc = VectorSearchService(
        VectorSearchConfig(dim=small_clustered.dim, backend=backend, ef_search=96)
    )
    svc.upsert_many(
        small_clustered.ids,
        [small_clustered.vectors[i].tolist() for i in range(small_clustered.n)],
        metadatas=small_clustered.metadatas,
    )
    assert len(svc) == small_clustered.n
    res = svc.search(small_clustered.vectors[0].tolist(), 1)
    assert res[0].id == small_clustered.ids[0]


def test_quantized_backend_needs_training(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim, backend="pq", pq_m=12))
    assert svc.needs_training
    svc.train([small_clustered.vectors[i].tolist() for i in range(small_clustered.n)])
    assert not svc.needs_training
    svc.upsert_many(
        small_clustered.ids,
        [small_clustered.vectors[i].tolist() for i in range(small_clustered.n)],
    )
    res = svc.search(small_clustered.vectors[0].tolist(), 5)
    assert len(res) == 5


# --------------------------------------------------------------------------- #
# Filtering + hybrid
# --------------------------------------------------------------------------- #


def test_metadata_filtered_search(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim, ef_search=128))
    svc.upsert_many(
        small_clustered.ids,
        [small_clustered.vectors[i].tolist() for i in range(small_clustered.n)],
        metadatas=small_clustered.metadatas,
    )
    res = svc.search(small_clustered.queries[0].tolist(), 10, where={"book": "book_1"})
    assert len(res) > 0
    assert all(r.metadata is not None and r.metadata["book"] == "book_1" for r in res)


def test_hybrid_query_fuses_keyword(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim, ef_search=128))
    svc.upsert_many(
        small_clustered.ids,
        [small_clustered.vectors[i].tolist() for i in range(small_clustered.n)],
        metadatas=small_clustered.metadatas,
    )
    q = Query(
        vector=small_clustered.queries[0].tolist(),
        k=10,
        text="cluster 3",
        alpha=0.5,
    )
    res = svc.query(q)
    assert len(res) > 0
    # scores are fused (≤1) and the list is closest-first.
    assert all(res[i].distance <= res[i + 1].distance for i in range(len(res) - 1))


def test_hybrid_keyword_rescues_exact_term(small_clustered: Corpus) -> None:
    """A doc whose text matches the query keyword should surface even if its
    vector is not in the dense top-k (the point of hybrid fusion)."""
    svc = VectorSearchService(VectorSearchConfig(dim=4, ef_search=64))
    # Four orthogonal vectors; the query points at v0.
    svc.upsert("v0", [1, 0, 0, 0], metadata={"text": "alpha"})
    svc.upsert("v1", [0, 1, 0, 0], metadata={"text": "beta unicorn"})
    svc.upsert("v2", [0, 0, 1, 0], metadata={"text": "gamma"})
    svc.upsert("v3", [0, 0, 0, 1], metadata={"text": "delta"})
    # Pure vector: v1 (unicorn) would not rank for a query at v0.
    q = Query(vector=[1, 0, 0, 0], k=4, text="unicorn", alpha=0.5)
    res = svc.query(q)
    ids = [r.id for r in res]
    assert "v1" in ids  # rescued by the keyword match


def test_query_pure_vector_when_alpha_one(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim, ef_search=96))
    svc.upsert_many(
        small_clustered.ids,
        [small_clustered.vectors[i].tolist() for i in range(small_clustered.n)],
        metadatas=small_clustered.metadatas,
    )
    q = Query(vector=small_clustered.vectors[0].tolist(), k=5, text="cluster 3", alpha=1.0)
    res = svc.query(q)
    assert res[0].id == small_clustered.ids[0]  # alpha=1 → pure ANN


def test_delete_removes_from_vector_and_keyword(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim))
    svc.upsert("a", small_clustered.vectors[0].tolist(), metadata={"text": "magic word"})
    assert "a" in svc
    assert svc.delete("a") is True
    assert "a" not in svc
    q = Query(vector=small_clustered.vectors[0].tolist(), k=5, text="magic", alpha=0.5)
    assert all(r.id != "a" for r in svc.query(q))


def test_service_compact(small_clustered: Corpus) -> None:
    svc = VectorSearchService(VectorSearchConfig(dim=small_clustered.dim))
    svc.upsert_many(
        small_clustered.ids[:100],
        [small_clustered.vectors[i].tolist() for i in range(100)],
    )
    for i in range(0, 100, 3):
        svc.delete(small_clustered.ids[i])
    svc.compact()
    assert svc.index.num_deleted == 0
