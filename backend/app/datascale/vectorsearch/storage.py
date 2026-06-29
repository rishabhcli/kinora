"""Persistence for the HNSW index with an mmap-friendly vector layout.

An index is saved as a small *directory* (a "segment"):

* ``vectors.npy`` — the dense ``(n_nodes, dim)`` ``float32`` matrix, written with
  :func:`numpy.save` so it can be re-opened with ``mmap_mode='r'`` and paged in
  lazily rather than loaded whole. This is the bulk of the bytes and the part we
  most want memory-mapped.
* ``meta.json`` — everything else (params, the layered adjacency graph, the
  id↔node maps, tombstones, per-node metadata). Small and human-inspectable.

Saving the graph as JSON keeps the format debuggable and version-tolerant; the
vectors stay binary for size and mmap. :func:`save_index` / :func:`load_index`
round-trip a live :class:`HnswIndex`; :func:`save_index` is also used by the
sharded index to persist each shard into a sub-directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .hnsw import HnswIndex, HnswParams
from .types import FLOAT, Metric

VECTORS_FILE = "vectors.npy"
META_FILE = "meta.json"
FORMAT_VERSION = 1


def save_index(index: HnswIndex, path: str | Path) -> Path:
    """Persist ``index`` into directory ``path`` (created if missing)."""
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    vectors = np.ascontiguousarray(index._vector_view, dtype=FLOAT)
    np.save(out / VECTORS_FILE, vectors, allow_pickle=False)
    meta = index._export()
    meta["format_version"] = FORMAT_VERSION
    (out / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return out


def load_index(path: str | Path, *, mmap: bool = True) -> HnswIndex:
    """Reconstruct an :class:`HnswIndex` from a saved segment directory.

    When ``mmap`` the vector matrix is opened read-only and memory-mapped, then
    copied into the index's writable buffer only if the index will be mutated;
    here we keep a writable copy (HNSW needs to append) but size the buffer to
    the file so a huge cold index pages in lazily during the copy.
    """
    src = Path(path)
    meta = json.loads((src / META_FILE).read_text(encoding="utf-8"))
    version = int(meta.get("format_version", 0))
    if version != FORMAT_VERSION:
        raise ValueError(f"unsupported segment format version: {version}")

    vectors: NDArray[np.float32] = np.load(
        src / VECTORS_FILE, mmap_mode="r" if mmap else None, allow_pickle=False
    )

    params = HnswParams(
        m=int(meta["params"]["m"]),
        ef_construction=int(meta["params"]["ef_construction"]),
        ef_search=int(meta["params"]["ef_search"]),
        ml=meta["params"]["ml"],
        seed=int(meta["params"]["seed"]),
    )
    idx = HnswIndex(
        int(meta["dim"]),
        metric=Metric(meta["metric"]),
        params=params,
        capacity=max(int(meta["n_nodes"]), 1),
    )
    _restore(idx, meta, vectors)
    return idx


def _restore(idx: HnswIndex, meta: dict[str, Any], vectors: NDArray[np.float32]) -> None:
    """Populate a fresh index's internals from a parsed segment (no re-build)."""
    n = int(meta["n_nodes"])
    idx._ensure_capacity(max(n, 1))
    if n:
        idx._vectors[:n] = np.asarray(vectors, dtype=FLOAT)
    idx._levels = [int(x) for x in meta["levels"]]
    idx._graph = [
        {int(k): [int(x) for x in v] for k, v in layer.items()} for layer in meta["graph"]
    ]
    if not idx._graph:
        idx._graph = [{}]
    idx._entry = None if meta["entry"] is None else int(meta["entry"])
    idx._deleted = {int(x) for x in meta["deleted"]}
    idx._ext_to_node = {str(k): int(v) for k, v in meta["ext_to_node"].items()}
    idx._node_to_ext = {v: k for k, v in idx._ext_to_node.items()}
    idx._meta = {int(k): v for k, v in meta.get("meta", {}).items()}
    idx._size = int(meta["size"])


def serialize_index(index: HnswIndex) -> dict[str, Any]:
    """In-memory serialisation (graph + vectors as nested lists) for transport.

    Heavier than the file format (vectors become JSON) but handy for tests,
    snapshots, and shipping a shard over a wire without a shared filesystem.
    """
    payload = index._export()
    payload["format_version"] = FORMAT_VERSION
    payload["vectors"] = index._vector_view.tolist()
    return payload


def deserialize_index(payload: dict[str, Any]) -> HnswIndex:
    """Inverse of :func:`serialize_index`."""
    params = HnswParams(
        m=int(payload["params"]["m"]),
        ef_construction=int(payload["params"]["ef_construction"]),
        ef_search=int(payload["params"]["ef_search"]),
        ml=payload["params"]["ml"],
        seed=int(payload["params"]["seed"]),
    )
    idx = HnswIndex(
        int(payload["dim"]),
        metric=Metric(payload["metric"]),
        params=params,
        capacity=max(int(payload["n_nodes"]), 1),
    )
    vectors = (
        np.asarray(payload.get("vectors", []), dtype=FLOAT).reshape(
            int(payload["n_nodes"]), int(payload["dim"])
        )
        if payload["n_nodes"]
        else np.empty((0, int(payload["dim"])), dtype=FLOAT)
    )
    _restore(idx, payload, vectors)
    return idx


__all__ = [
    "FORMAT_VERSION",
    "META_FILE",
    "VECTORS_FILE",
    "deserialize_index",
    "load_index",
    "save_index",
    "serialize_index",
]
