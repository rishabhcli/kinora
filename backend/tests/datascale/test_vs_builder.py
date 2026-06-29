"""Tests for batch builders and the incremental builder + compaction policy."""

from __future__ import annotations

from app.datascale.vectorsearch.builder import (
    IncrementalBuilder,
    auto_params,
    build_hnsw,
    build_quantized,
    build_sharded,
)
from app.datascale.vectorsearch.hnsw import HnswIndex
from app.datascale.vectorsearch.pq_index import QuantizedFlatIndex
from app.datascale.vectorsearch.shard import ShardedIndex
from app.datascale.vectorsearch.types import Metric

from .conftest import Corpus


def test_auto_params_scales_with_n() -> None:
    small = auto_params(500)
    mid = auto_params(50_000)
    big = auto_params(1_000_000)
    assert small.m <= mid.m <= big.m
    assert small.ef_construction <= mid.ef_construction <= big.ef_construction


def test_auto_params_respects_explicit_base() -> None:
    from app.datascale.vectorsearch.hnsw import HnswParams

    base = HnswParams(m=99, ef_construction=7, ef_search=3)
    assert auto_params(10, base=base) is base


def test_build_hnsw(small_clustered: Corpus) -> None:
    idx = build_hnsw(
        small_clustered.ids,
        small_clustered.rows(),
        dim=small_clustered.dim,
        metadatas=small_clustered.metadatas,
    )
    assert isinstance(idx, HnswIndex)
    assert len(idx) == small_clustered.n
    assert idx.search(small_clustered.queries[0], 5)


def test_build_sharded(small_clustered: Corpus) -> None:
    idx = build_sharded(
        small_clustered.ids, small_clustered.rows(), n_shards=3, dim=small_clustered.dim
    )
    assert isinstance(idx, ShardedIndex)
    assert sum(idx.shard_sizes()) == small_clustered.n


def test_build_quantized_trains_and_encodes(small_clustered: Corpus) -> None:
    idx = build_quantized(
        small_clustered.ids,
        small_clustered.rows(),
        dim=small_clustered.dim,
        kind="pq",
        m=12,
        nbits=8,
    )
    assert isinstance(idx, QuantizedFlatIndex)
    assert idx.is_trained
    assert len(idx) == small_clustered.n


def test_build_metric_propagates(small_clustered: Corpus) -> None:
    idx = build_hnsw(
        small_clustered.ids, small_clustered.rows(), dim=small_clustered.dim, metric=Metric.L2
    )
    assert idx.metric is Metric.L2


# --------------------------------------------------------------------------- #
# Incremental builder
# --------------------------------------------------------------------------- #


def test_incremental_add_and_query(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim)
    for i in range(200):
        b.add(small_clustered.ids[i], small_clustered.vectors[i])
    assert len(b.index) == 200
    res = b.index.search(small_clustered.vectors[0], 1)
    assert res[0].id == small_clustered.ids[0]


def test_incremental_add_batch(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim)
    b.add_batch(
        small_clustered.ids[:100],
        [small_clustered.vectors[i] for i in range(100)],
        metadatas=[small_clustered.metadatas[i] for i in range(100)],
    )
    assert len(b.index) == 100


def test_compaction_policy_by_count(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim, compact_after=10, compact_fraction=2.0)
    for i in range(100):
        b.add(small_clustered.ids[i], small_clustered.vectors[i])
    for i in range(10):
        b.remove(small_clustered.ids[i])
    assert b.should_compact()  # 10 removals hit compact_after
    assert b.maybe_compact() is True
    assert b.index.num_deleted == 0
    assert len(b.index) == 90


def test_compaction_policy_by_fraction(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim, compact_after=10_000, compact_fraction=0.2)
    for i in range(100):
        b.add(small_clustered.ids[i], small_clustered.vectors[i])
    for i in range(30):  # 30% deleted > 0.2 fraction
        b.remove(small_clustered.ids[i])
    assert b.should_compact()
    assert b.maybe_compact() is True
    assert len(b.index) == 70


def test_no_compaction_when_clean(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim)
    for i in range(50):
        b.add(small_clustered.ids[i], small_clustered.vectors[i])
    assert not b.should_compact()
    assert b.maybe_compact() is False


def test_force_compact(small_clustered: Corpus) -> None:
    b = IncrementalBuilder(small_clustered.dim)
    for i in range(50):
        b.add(small_clustered.ids[i], small_clustered.vectors[i])
    b.remove(small_clustered.ids[0])
    b.force_compact()
    assert b.index.num_deleted == 0
    assert len(b.index) == 49
