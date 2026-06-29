"""Tests for the sharded index (router + fan-out) and the k-way result merge."""

from __future__ import annotations

import pytest

from app.datascale.vectorsearch.brute_force import BruteForceIndex
from app.datascale.vectorsearch.merge import (
    merge_dedup_keep_closest,
    merge_results,
)
from app.datascale.vectorsearch.shard import (
    AttributeRouter,
    HashRouter,
    ModuloRouter,
    ShardedIndex,
    rebalance_plan,
)
from app.datascale.vectorsearch.types import SearchResult

from .conftest import Corpus

# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


def _r(vid: str, d: float) -> SearchResult:
    return SearchResult(id=vid, distance=d, score=-d)


def test_merge_results_global_order() -> None:
    a = [_r("a", 0.1), _r("c", 0.3), _r("e", 0.7)]
    b = [_r("b", 0.2), _r("d", 0.5), _r("f", 0.9)]
    out = merge_results([a, b], 4)
    assert [r.id for r in out] == ["a", "b", "c", "d"]


def test_merge_results_dedups_keeping_closer() -> None:
    # Each run is independently sorted (the index guarantees this).
    a = [_r("x", 0.1), _r("y", 0.4)]
    b = [_r("z", 0.2), _r("x", 0.9)]
    out = merge_results([a, b], 5)
    ids = [r.id for r in out]
    assert ids == ["x", "z", "y"]
    assert out[0].distance == 0.1  # closer copy of x wins (dedup on first sight)


def test_merge_results_deterministic_ties() -> None:
    a = [_r("a", 0.5)]
    b = [_r("b", 0.5)]
    out = merge_results([b, a], 2)
    assert [r.id for r in out] == ["a", "b"]  # id breaks the tie


def test_merge_dedup_keep_closest_unsorted() -> None:
    a = [_r("p", 0.6), _r("q", 0.1)]
    b = [_r("q", 0.05), _r("r", 0.3)]
    out = merge_dedup_keep_closest([a, b], 2)
    assert [r.id for r in out] == ["q", "r"]
    assert out[0].distance == 0.05


def test_merge_empty() -> None:
    assert merge_results([], 5) == []
    assert merge_results([[], []], 5) == []


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #


def test_hash_router_stable_and_in_range() -> None:
    r = HashRouter(8)
    for vid in ("alpha", "beta", "gamma", "shot_42"):
        s = r.route(vid)
        assert 0 <= s < 8
        assert s == r.route(vid)  # stable


def test_modulo_router_with_injected_hash() -> None:
    r = ModuloRouter(4, hash_fn=lambda s: len(s))
    assert r.route("ab") == 2
    assert r.route("abcd") == 0


def test_attribute_router_prunes_query_shards() -> None:
    r = AttributeRouter(6, field="book")
    shards = r.query_shards({"book": "book_1"})
    assert len(shards) == 1
    # Same value always routes the same way (so the prune is correct).
    assert r.route("x", {"book": "book_1"}) == shards[0]
    # $eq form is honoured too.
    assert r.query_shards({"book": {"$eq": "book_1"}}) == shards
    # A non-pinning filter falls back to all shards.
    assert len(r.query_shards({"book": {"$in": ["a", "b"]}})) == 6


def test_rebalance_plan() -> None:
    assert rebalance_plan([10, 10, 10, 10]) == pytest.approx(1.0)
    assert rebalance_plan([40, 10, 10, 10]) > 2.0
    assert rebalance_plan([]) == 1.0


# --------------------------------------------------------------------------- #
# Sharded index
# --------------------------------------------------------------------------- #


def test_sharded_recall_matches_single_index(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=4)
    idx.add_many(clustered.ids, clustered.rows(), metadatas=clustered.metadatas)
    bf = BruteForceIndex(clustered.dim)
    bf.add_many(clustered.ids, clustered.rows())
    hit = total = 0
    for qi in range(clustered.queries.shape[0]):
        q = clustered.queries[qi]
        truth = set(bf.exact_neighbors(q, 10))
        got = {r.id for r in idx.search(q, 10, ef=120, per_shard_k=20)}
        hit += len(truth & got)
        total += len(truth)
    assert hit / total >= 0.90


def test_sharded_distributes_evenly(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=4)
    idx.add_many(clustered.ids, clustered.rows())
    sizes = idx.shard_sizes()
    assert sum(sizes) == clustered.n
    assert rebalance_plan(sizes) < 1.15  # hash routing is fairly balanced


def test_sharded_id_lives_on_one_shard(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=3)
    idx.add_many(clustered.ids[:300], [clustered.vectors[i] for i in range(300)])
    owner = idx.shard_of("v0")
    assert owner is not None
    # Re-add stays on the same shard.
    idx.add("v0", clustered.vectors[1])
    assert idx.shard_of("v0") == owner


def test_sharded_delete(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=3)
    idx.add_many(clustered.ids[:100], [clustered.vectors[i] for i in range(100)])
    assert "v0" in idx
    assert idx.remove("v0") is True
    assert "v0" not in idx
    assert idx.remove("v0") is False


def test_attribute_router_query_visits_one_shard(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=5, router=AttributeRouter(5, "book"))
    idx.add_many(clustered.ids, clustered.rows(), metadatas=clustered.metadatas)
    # All book_2 vectors must land on one shard; a filtered query finds them.
    res = idx.search(clustered.queries[0], 10, ef=120, where={"book": "book_2"})
    assert len(res) > 0
    assert all(r.metadata is not None and r.metadata["book"] == "book_2" for r in res)


def test_sharded_compact(clustered: Corpus) -> None:
    idx = ShardedIndex(clustered.dim, n_shards=2)
    idx.add_many(clustered.ids[:200], [clustered.vectors[i] for i in range(200)])
    for i in range(0, 200, 4):
        idx.remove(clustered.ids[i])
    before = len(idx)
    idx.compact()
    assert len(idx) == before
    # still queryable post-compaction
    assert len(idx.search(clustered.queries[0], 5)) == 5


def test_router_shard_count_must_match() -> None:
    with pytest.raises(ValueError):
        ShardedIndex(8, n_shards=4, router=HashRouter(3))
