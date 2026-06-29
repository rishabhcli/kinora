"""HNSW correctness + recall@k vs exact search on seeded data.

These are the load-bearing recall assertions: the approximate graph must recover
a high fraction of the *exact* top-k that the brute-force index returns, on data
fixed by a seed so the threshold is stable.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.datascale.vectorsearch.benchmark import benchmark as run_benchmark
from app.datascale.vectorsearch.brute_force import BruteForceIndex
from app.datascale.vectorsearch.hnsw import HnswIndex, HnswParams
from app.datascale.vectorsearch.types import Metric

from .conftest import Corpus


def _build(corpus: Corpus, *, metric: Metric = Metric.COSINE, **pkw: int) -> HnswIndex:
    params = HnswParams(seed=0, **pkw) if pkw else HnswParams(seed=0)
    idx = HnswIndex(corpus.dim, metric=metric, params=params, capacity=corpus.n)
    idx.add_many(corpus.ids, corpus.rows(), metadatas=corpus.metadatas)
    return idx


def test_recall_at_10_clustered(clustered: Corpus) -> None:
    idx = _build(clustered, m=16, ef_construction=200, ef_search=64)
    report = run_benchmark(
        idx,
        clustered.ids,
        clustered.vectors,
        clustered.queries,
        k=10,
        search_kwargs={"ef": 100},
    )
    assert report.recall_at_k >= 0.92, report.recall_at_k
    assert report.map_at_k >= 0.85


def test_recall_at_10_isotropic_hard_case(isotropic: Corpus) -> None:
    idx = _build(isotropic, m=16, ef_construction=200, ef_search=80)
    report = run_benchmark(
        idx,
        isotropic.ids,
        isotropic.vectors,
        isotropic.queries,
        k=10,
        search_kwargs={"ef": 120},
    )
    # Isotropic data is the hard case but a wide beam still recovers most.
    assert report.recall_at_k >= 0.85, report.recall_at_k


def test_higher_ef_improves_recall(clustered: Corpus) -> None:
    idx = _build(clustered, m=12, ef_construction=150, ef_search=16)
    low = run_benchmark(
        idx,
        clustered.ids,
        clustered.vectors,
        clustered.queries,
        k=10,
        search_kwargs={"ef": 16},
    )
    high = run_benchmark(
        idx,
        clustered.ids,
        clustered.vectors,
        clustered.queries,
        k=10,
        search_kwargs={"ef": 200},
    )
    assert high.recall_at_k >= low.recall_at_k


def test_l2_metric_recall(clustered: Corpus) -> None:
    idx = _build(clustered, metric=Metric.L2, m=16, ef_construction=200, ef_search=64)
    report = run_benchmark(
        idx,
        clustered.ids,
        clustered.vectors,
        clustered.queries,
        k=10,
        metric=Metric.L2,
        search_kwargs={"ef": 120},
    )
    assert report.recall_at_k >= 0.90, report.recall_at_k


def test_exact_query_returns_self_first(small_clustered: Corpus) -> None:
    idx = _build(small_clustered, m=16, ef_construction=200, ef_search=64)
    # Querying a stored vector should return that id as the nearest neighbour.
    for i in (0, 100, 250, 599):
        res = idx.search(small_clustered.vectors[i], 1, ef=64)
        assert res[0].id == small_clustered.ids[i]


def test_determinism_same_seed(small_clustered: Corpus) -> None:
    a = _build(small_clustered, m=16, ef_construction=120, ef_search=48)
    b = _build(small_clustered, m=16, ef_construction=120, ef_search=48)
    q = small_clustered.queries[0]
    assert [r.id for r in a.search(q, 10)] == [r.id for r in b.search(q, 10)]


def test_graph_structure_grows_layers(clustered: Corpus) -> None:
    idx = _build(clustered, m=16, ef_construction=200, ef_search=64)
    stats = idx.stats()
    assert stats.live == clustered.n
    assert stats.levels >= 2  # multi-layer for thousands of nodes
    assert 0 < stats.avg_degree_l0 <= idx.params.m0
    assert stats.entry is not None


def test_delete_masks_results_and_repairs(small_clustered: Corpus) -> None:
    idx = _build(small_clustered, m=16, ef_construction=200, ef_search=64)
    # Delete every even id; results must contain only odd ids afterwards.
    for i in range(0, small_clustered.n, 2):
        assert idx.remove(small_clustered.ids[i])
    assert len(idx) == small_clustered.n // 2
    assert idx.num_deleted == small_clustered.n // 2
    for qi in range(5):
        res = idx.search(small_clustered.queries[qi], 10, ef=80)
        assert all(int(r.id[1:]) % 2 == 1 for r in res)


def test_delete_then_recall_still_high(clustered: Corpus) -> None:
    idx = _build(clustered, m=16, ef_construction=200, ef_search=64)
    # Remove a random 30% then check recall against the *remaining* exact set.
    rng = np.random.default_rng(42)
    drop = set(rng.choice(clustered.n, size=clustered.n * 3 // 10, replace=False).tolist())
    for i in drop:
        idx.remove(clustered.ids[i])
    keep_ids = [clustered.ids[i] for i in range(clustered.n) if i not in drop]
    keep_vecs = np.vstack([clustered.vectors[i] for i in range(clustered.n) if i not in drop])
    report = run_benchmark(
        idx, keep_ids, keep_vecs, clustered.queries[:25], k=10, search_kwargs={"ef": 120}
    )
    assert report.recall_at_k >= 0.85, report.recall_at_k


def test_compact_reclaims_and_preserves_results(small_clustered: Corpus) -> None:
    idx = _build(small_clustered, m=16, ef_construction=200, ef_search=64)
    for i in range(0, small_clustered.n, 3):
        idx.remove(small_clustered.ids[i])
    before = [r.id for r in idx.search(small_clustered.queries[0], 10, ef=80)]
    compacted = idx.compact()
    assert compacted.num_deleted == 0
    assert len(compacted) == len(idx)
    after = [r.id for r in compacted.search(small_clustered.queries[0], 10, ef=80)]
    # Compaction must not change which neighbours win (same live set, same seed math).
    assert set(after) == set(before)


def test_update_existing_id_changes_neighbourhood() -> None:
    idx = HnswIndex(4, metric=Metric.COSINE, params=HnswParams(seed=1))
    idx.add("a", [1.0, 0.0, 0.0, 0.0])
    idx.add("b", [0.0, 1.0, 0.0, 0.0])
    idx.add("c", [0.0, 0.0, 1.0, 0.0])
    assert idx.search([1.0, 0.0, 0.0, 0.0], 1)[0].id == "a"
    idx.add("a", [0.0, 0.0, 0.0, 1.0])  # move a far away
    assert idx.search([1.0, 0.0, 0.0, 0.0], 1)[0].id != "a"
    assert len(idx) == 3  # still one 'a'


def test_metadata_post_filter(clustered: Corpus) -> None:
    idx = _build(clustered, m=16, ef_construction=200, ef_search=64)
    res = idx.search(clustered.queries[0], 10, ef=128, where={"book": "book_2"})
    assert len(res) > 0
    assert all(r.metadata is not None and r.metadata["book"] == "book_2" for r in res)


def test_empty_index_returns_nothing() -> None:
    idx = HnswIndex(8)
    assert idx.search([0.0] * 8, 5) == []


def test_params_validation() -> None:
    with pytest.raises(ValueError):
        HnswParams(m=1)
    with pytest.raises(ValueError):
        HnswParams(ef_construction=0)


def test_single_vector_index() -> None:
    idx = HnswIndex(3)
    idx.add("only", [1.0, 2.0, 3.0])
    res = idx.search([1.0, 2.0, 3.0], 5)
    assert len(res) == 1 and res[0].id == "only"


def test_recall_matches_brute_force_membership(small_clustered: Corpus) -> None:
    """Direct per-query overlap with BruteForceIndex (not just the benchmark)."""
    idx = _build(small_clustered, m=16, ef_construction=200, ef_search=64)
    bf = BruteForceIndex(small_clustered.dim)
    bf.add_many(small_clustered.ids, small_clustered.rows())
    total = 0
    hit = 0
    for qi in range(small_clustered.queries.shape[0]):
        q = small_clustered.queries[qi]
        truth = set(bf.exact_neighbors(q, 10))
        got = {r.id for r in idx.search(q, 10, ef=100)}
        hit += len(truth & got)
        total += len(truth)
    assert hit / total >= 0.92
