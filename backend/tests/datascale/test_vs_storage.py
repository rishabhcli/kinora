"""Tests for index persistence (file segment, mmap, serialize round-trip)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.datascale.vectorsearch import storage
from app.datascale.vectorsearch.builder import build_hnsw
from app.datascale.vectorsearch.shard import ShardedIndex
from app.datascale.vectorsearch.storage import (
    META_FILE,
    VECTORS_FILE,
    deserialize_index,
    load_index,
    save_index,
    serialize_index,
)

from .conftest import Corpus


def test_save_load_round_trip_identical(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(
        small_clustered.ids,
        small_clustered.rows(),
        dim=small_clustered.dim,
        metadatas=small_clustered.metadatas,
    )
    seg = tmp_path / "seg"
    save_index(idx, seg)
    assert (seg / VECTORS_FILE).exists() and (seg / META_FILE).exists()
    loaded = load_index(seg, mmap=True)
    q = small_clustered.queries[0]
    assert [r.id for r in loaded.search(q, 10)] == [r.id for r in idx.search(q, 10)]
    # metadata survives
    res = loaded.search(q, 5)
    assert all(r.metadata is not None for r in res)


def test_load_preserves_tombstones(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(small_clustered.ids, small_clustered.rows(), dim=small_clustered.dim)
    idx.remove("v0")
    idx.remove("v1")
    save_index(idx, tmp_path / "seg")
    loaded = load_index(tmp_path / "seg")
    assert "v0" not in loaded and "v1" not in loaded
    assert len(loaded) == len(idx)


def test_mmap_false_also_works(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(small_clustered.ids, small_clustered.rows(), dim=small_clustered.dim)
    save_index(idx, tmp_path / "seg")
    loaded = load_index(tmp_path / "seg", mmap=False)
    q = small_clustered.queries[0]
    assert [r.id for r in loaded.search(q, 10)] == [r.id for r in idx.search(q, 10)]


def test_loaded_index_is_mutable(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(
        small_clustered.ids[:100],
        [small_clustered.vectors[i] for i in range(100)],
        dim=small_clustered.dim,
    )
    save_index(idx, tmp_path / "seg")
    loaded = load_index(tmp_path / "seg", mmap=True)
    loaded.add("new_vec", small_clustered.vectors[200])
    assert "new_vec" in loaded
    res = loaded.search(small_clustered.vectors[200], 1)
    assert res[0].id == "new_vec"


def test_serialize_deserialize_round_trip(small_clustered: Corpus) -> None:
    idx = build_hnsw(
        small_clustered.ids,
        small_clustered.rows(),
        dim=small_clustered.dim,
        metadatas=small_clustered.metadatas,
    )
    payload = serialize_index(idx)
    restored = deserialize_index(payload)
    q = small_clustered.queries[0]
    assert [r.id for r in restored.search(q, 10)] == [r.id for r in idx.search(q, 10)]


def test_bad_format_version_rejected(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(
        small_clustered.ids[:10],
        [small_clustered.vectors[i] for i in range(10)],
        dim=small_clustered.dim,
    )
    seg = tmp_path / "seg"
    save_index(idx, seg)
    import json

    meta = json.loads((seg / META_FILE).read_text())
    meta["format_version"] = 999
    (seg / META_FILE).write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        load_index(seg)


def test_sharded_save_load(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = ShardedIndex(small_clustered.dim, n_shards=3)
    idx.add_many(small_clustered.ids, small_clustered.rows(), metadatas=small_clustered.metadatas)
    idx.save(tmp_path / "sharded")
    loaded = ShardedIndex.load(tmp_path / "sharded", mmap=True)
    assert len(loaded) == len(idx)
    q = small_clustered.queries[0]
    assert {r.id for r in loaded.search(q, 10)} == {r.id for r in idx.search(q, 10)}


def test_vectors_file_is_npy_and_mmapable(small_clustered: Corpus, tmp_path: Path) -> None:
    idx = build_hnsw(
        small_clustered.ids[:50],
        [small_clustered.vectors[i] for i in range(50)],
        dim=small_clustered.dim,
    )
    save_index(idx, tmp_path / "seg")
    arr = np.load(tmp_path / "seg" / VECTORS_FILE, mmap_mode="r")
    assert arr.shape == (50, small_clustered.dim)
    assert arr.dtype == np.float32


def test_format_version_constant() -> None:
    assert isinstance(storage.FORMAT_VERSION, int)
