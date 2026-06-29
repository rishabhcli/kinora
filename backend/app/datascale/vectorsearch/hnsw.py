"""A from-scratch Hierarchical Navigable Small World (HNSW) index.

HNSW (Malkov & Yashunin, 2016) is a multi-layer proximity graph: upper layers
are sparse "express lanes" that get the search near the query in O(log n) hops,
the bottom layer (L0) is dense and does the fine kNN. This implementation is
pure Python + NumPy, deterministic given a seed, and supports the full lifecycle
the prompt asks for — insert, search, delete (via tombstones + repair), tunable
``M`` / ``ef_construction`` / ``ef``, and a flat array layout that
:mod:`app.datascale.vectorsearch.storage` can persist and mmap.

Design notes
------------
* **Internal integer node ids.** External ``VectorId`` strings map to dense
  ``int`` node ids; vectors live in one growable ``(capacity, dim)`` matrix so a
  candidate's distance is a single ``numpy`` row op and the matrix is directly
  mmap-friendly on disk.
* **One ordering convention.** Everything uses the "smaller is closer" ordering
  key from :mod:`distance`, so cosine/dot (similarities) and L2 (distance) share
  one heap discipline. Candidate heaps are min-heaps on that key; the dynamic
  result set is a max-heap (negated key) so the *farthest* current neighbour is
  O(1) to pop when the set overflows ``ef``.
* **Heuristic neighbour selection.** Edges are pruned with the §4 "select
  neighbours heuristic" (keep an edge only if the candidate is closer to the new
  node than to any already-kept neighbour) which yields the long-range links a
  navigable graph needs — far better recall than naive nearest-M.
* **Deletes are tombstones + repair.** A deleted node is masked from results and
  its in-neighbours are re-linked so the graph stays connected; the slot is
  reclaimed by :meth:`compact`.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import distance as dist
from .filtering import Predicate
from .types import FLOAT, Metadata, Metric, SearchResult, VectorId, as_vector


@dataclass(slots=True)
class HnswParams:
    """Tunable HNSW hyper-parameters.

    ``m`` is the max out-degree on layers ≥1; the dense bottom layer gets
    ``m0 = 2·m`` by convention. ``ef_construction`` is the build-time beam width;
    ``ef_search`` the default query beam (a query may override per-call).
    ``ml`` is the level-assignment normaliser (``1/ln(m)`` is the standard).
    """

    m: int = 16
    ef_construction: int = 200
    ef_search: int = 50
    ml: float | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        if self.m < 2:
            raise ValueError("m must be >= 2")
        if self.ef_construction < 1 or self.ef_search < 1:
            raise ValueError("ef_construction and ef_search must be >= 1")
        if self.ml is None:
            self.ml = 1.0 / math.log(self.m)

    @property
    def m0(self) -> int:
        return self.m * 2

    @property
    def ml_value(self) -> float:
        """The level normaliser, guaranteed non-None after ``__post_init__``."""
        return self.ml if self.ml is not None else 1.0 / math.log(self.m)


class HnswIndex:
    """An incrementally-built, deletable HNSW graph over ``float32`` vectors."""

    def __init__(
        self,
        dim: int,
        *,
        metric: Metric = Metric.COSINE,
        params: HnswParams | None = None,
        capacity: int = 1024,
    ) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self.metric = metric
        self.params = params or HnswParams()
        self._rng = np.random.default_rng(self.params.seed)

        # Storage: dense vector matrix + per-node bookkeeping.
        self._cap = max(capacity, 1)
        self._vectors: NDArray[np.float32] = np.zeros((self._cap, dim), dtype=FLOAT)
        self._levels: list[int] = []  # node -> top layer
        # graph[layer] is a dict: node -> list[neighbour node].
        self._graph: list[dict[int, list[int]]] = [{}]
        self._meta: dict[int, Metadata] = {}
        self._deleted: set[int] = set()

        # id <-> internal node maps.
        self._ext_to_node: dict[VectorId, int] = {}
        self._node_to_ext: dict[int, VectorId] = {}
        self._entry: int | None = None  # entry point node id
        self._size = 0  # live (non-deleted) count

    # -- properties --------------------------------------------------------- #
    @property
    def max_level(self) -> int:
        """Index of the topmost populated layer."""
        return len(self._graph) - 1

    def __len__(self) -> int:
        return self._size

    def __contains__(self, vid: object) -> bool:
        if not isinstance(vid, str):
            return False
        node = self._ext_to_node.get(vid)
        return node is not None and node not in self._deleted

    def ids(self) -> Iterable[VectorId]:
        return tuple(self._node_to_ext[n] for n in self._node_to_ext if n not in self._deleted)

    # -- internal helpers --------------------------------------------------- #
    def _ensure_capacity(self, needed: int) -> None:
        if needed <= self._cap:
            return
        new_cap = self._cap
        while new_cap < needed:
            new_cap *= 2
        grown = np.zeros((new_cap, self.dim), dtype=FLOAT)
        grown[: self._vectors.shape[0]] = self._vectors
        self._vectors = grown
        self._cap = new_cap

    def _random_level(self) -> int:
        # Exponentially-decaying layer assignment: floor(-ln(U) * ml).
        u = max(self._rng.random(), 1e-12)
        return int(-math.log(u) * self.params.ml_value)

    def _dist_to(self, query: NDArray[np.float32], node: int) -> float:
        return float(dist.order_value(query, self._vectors[node], self.metric))

    def _dist_many(self, query: NDArray[np.float32], nodes: list[int]) -> NDArray[np.float32]:
        if not nodes:
            return np.empty((0,), dtype=FLOAT)
        return dist.order_value_batch(query, self._vectors[nodes], self.metric)

    # -- search layer (the core graph walk) -------------------------------- #
    def _search_layer(
        self,
        query: NDArray[np.float32],
        entry_points: list[int],
        ef: int,
        layer: int,
        *,
        allow_deleted: bool,
    ) -> list[tuple[float, int]]:
        """Greedy best-first beam search on one layer.

        Returns up to ``ef`` ``(order_key, node)`` pairs nearest to ``query``.
        ``candidates`` is a min-heap (closest first); ``results`` is a max-heap
        keyed on the negated order so the farthest kept result is the heap top.
        """
        visited: set[int] = set(entry_points)
        graph = self._graph[layer]
        candidates: list[tuple[float, int]] = []
        results: list[tuple[float, int]] = []
        for ep in entry_points:
            d = self._dist_to(query, ep)
            heapq.heappush(candidates, (d, ep))
            if allow_deleted or ep not in self._deleted:
                heapq.heappush(results, (-d, ep))

        while candidates:
            dist_c, c = heapq.heappop(candidates)
            # Stop when the closest remaining candidate is farther than the
            # current worst kept result and we already have ef results.
            if results and dist_c > -results[0][0] and len(results) >= ef:
                break
            neighbours = graph.get(c, ())
            fresh = [n for n in neighbours if n not in visited]
            visited.update(fresh)
            if not fresh:
                continue
            dists = self._dist_many(query, fresh)
            worst = -results[0][0] if results else math.inf
            for n, dn in zip(fresh, dists.tolist(), strict=True):
                keep = (not results) or dn < worst or len(results) < ef
                if not keep:
                    continue
                heapq.heappush(candidates, (dn, n))
                if allow_deleted or n not in self._deleted:
                    heapq.heappush(results, (-dn, n))
                    if len(results) > ef:
                        heapq.heappop(results)
                        worst = -results[0][0]
                    elif len(results) == ef:
                        worst = -results[0][0]
        return [(-negd, node) for negd, node in results]

    def _select_neighbours_heuristic(
        self,
        base: NDArray[np.float32],
        candidates: list[tuple[float, int]],
        m: int,
        *,
        keep_pruned: bool = True,
    ) -> list[int]:
        """The §4 heuristic edge selection: relevant *and* diverse links.

        Accept a candidate only if it is closer to ``base`` than to every
        already-accepted neighbour — this is what creates the long-range edges a
        navigable graph relies on. ``keep_pruned`` backfills with the closest
        rejected candidates so degree is used fully.
        """
        if len(candidates) <= m:
            return [node for _, node in sorted(candidates)]
        working = sorted(candidates)  # closest first by order key
        chosen: list[int] = []
        pruned: list[tuple[float, int]] = []
        for dist_cb, cand in working:
            if len(chosen) >= m:
                break
            cand_vec = self._vectors[cand]
            ok = True
            for sel in chosen:
                d_cand_sel = float(dist.order_value(cand_vec, self._vectors[sel], self.metric))
                if d_cand_sel < dist_cb:  # closer to a kept node than to base
                    ok = False
                    break
            if ok:
                chosen.append(cand)
            else:
                pruned.append((dist_cb, cand))
        if keep_pruned:
            for _, cand in pruned:
                if len(chosen) >= m:
                    break
                chosen.append(cand)
        return chosen

    # -- insert ------------------------------------------------------------- #
    def add(self, vid: VectorId, vector: Any, *, metadata: Metadata | None = None) -> None:
        """Insert (or replace) the vector for external id ``vid``."""
        vec = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        if vid in self._ext_to_node:
            self._replace(vid, vec, metadata)
            return
        node = len(self._levels)
        self._ensure_capacity(node + 1)
        self._vectors[node] = vec
        level = self._random_level()
        self._levels.append(level)
        self._ext_to_node[vid] = node
        self._node_to_ext[node] = vid
        if metadata is not None:
            self._meta[node] = dict(metadata)
        self._size += 1

        while len(self._graph) <= level:
            self._graph.append({})
        for lyr in range(level + 1):
            self._graph[lyr].setdefault(node, [])

        if self._entry is None:
            self._entry = node
            return

        self._link_node(node, vec, level)
        if level > self._levels[self._entry]:
            self._entry = node

    def _link_node(self, node: int, vec: NDArray[np.float32], level: int) -> None:
        assert self._entry is not None
        ep = [self._entry]
        top = self._levels[self._entry]
        # Descend the express lanes to get near the insertion point.
        for lyr in range(top, level, -1):
            res = self._search_layer(vec, ep, 1, lyr, allow_deleted=True)
            if res:
                ep = [min(res)[1]]
        # Connect at each layer from the node's top down to L0.
        for lyr in range(min(level, top), -1, -1):
            m = self.params.m0 if lyr == 0 else self.params.m
            found = self._search_layer(
                vec, ep, self.params.ef_construction, lyr, allow_deleted=True
            )
            neighbours = self._select_neighbours_heuristic(vec, found, m)
            self._graph[lyr][node] = list(neighbours)
            for nb in neighbours:
                self._add_backlink(nb, node, lyr, m)
            ep = [n for _, n in sorted(found)] or ep

    def _add_backlink(self, owner: int, new: int, layer: int, m: int) -> None:
        """Add ``new`` to ``owner``'s adjacency on ``layer``, pruning to ``m``."""
        adj = self._graph[layer].setdefault(owner, [])
        if new in adj:
            return
        adj.append(new)
        if len(adj) <= m:
            return
        # Over-degree: re-select with the heuristic on the owner's neighbourhood.
        owner_vec = self._vectors[owner]
        cands = [
            (float(dist.order_value(owner_vec, self._vectors[n], self.metric)), n) for n in adj
        ]
        self._graph[layer][owner] = self._select_neighbours_heuristic(owner_vec, cands, m)

    def _replace(self, vid: VectorId, vec: NDArray[np.float32], metadata: Metadata | None) -> None:
        """Update an existing id's vector by delete-then-reinsert (simplest correct)."""
        node = self._ext_to_node[vid]
        meta = metadata if metadata is not None else self._meta.get(node)
        self.remove(vid)
        self.add(vid, vec, metadata=meta)

    def add_many(
        self,
        ids: Sequence[VectorId],
        vectors: Any,
        *,
        metadatas: Sequence[Metadata | None] | None = None,
    ) -> None:
        """Incrementally insert many vectors (one graph walk each)."""
        metas = metadatas or [None] * len(ids)
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.add(vid, vec, metadata=meta)

    # -- search ------------------------------------------------------------- #
    def search(
        self,
        vector: Any,
        k: int = 10,
        *,
        ef: int | None = None,
        where: Predicate | Mapping[str, Any] | None = None,
        oversample: int = 4,
    ) -> list[SearchResult]:
        """Approximate top-``k`` neighbours.

        When a metadata filter is given the search **post-filters**: it widens
        the beam (``ef`` and an oversampled candidate pool) and keeps only
        matching hits, so a selective filter still returns ``k`` results without
        a separate pre-filtered graph. Pre-filtering is available at the service
        layer for very selective predicates.
        """
        if k <= 0 or self._entry is None or self._size == 0:
            return []
        q = dist.maybe_normalize(as_vector(vector, dim=self.dim), self.metric)
        pred = Predicate.coerce(where)
        beam = ef or self.params.ef_search
        target = k if pred is None else k * max(oversample, 1)
        beam = max(beam, target)

        ep = [self._entry]
        for lyr in range(self.max_level, 0, -1):
            res = self._search_layer(q, ep, 1, lyr, allow_deleted=False)
            if res:
                ep = [min(res)[1]]
        found = self._search_layer(q, ep, beam, 0, allow_deleted=False)
        found.sort()

        out: list[SearchResult] = []
        for order_key, node in found:
            if node in self._deleted:
                continue
            meta = self._meta.get(node)
            if pred is not None and not pred.matches(meta):
                continue
            out.append(
                SearchResult(
                    id=self._node_to_ext[node],
                    distance=order_key,
                    score=dist.order_to_score(order_key, self.metric),
                    metadata=meta,
                )
            )
            if len(out) >= k:
                break
        return out

    # -- delete ------------------------------------------------------------- #
    def remove(self, vid: VectorId) -> bool:
        """Tombstone ``vid`` and repair its in-neighbours' connectivity.

        The node's slot is kept (so other nodes' integer ids stay valid) but it
        is masked from every result and its neighbours are cross-linked to keep
        the graph navigable. Call :meth:`compact` to reclaim slots.
        """
        node = self._ext_to_node.get(vid)
        if node is None or node in self._deleted:
            return False
        self._deleted.add(node)
        self._size -= 1
        # Free the external id so a later add(vid, ...) is a genuine new insert
        # rather than recursing through _replace; the tombstoned node keeps its
        # slot/vector for graph integrity until compaction. The node->id map is
        # retained so compact()/_pick_new_entry can still resolve live nodes;
        # the deleted node is filtered there.
        self._ext_to_node.pop(vid, None)
        node_level = self._levels[node]
        for lyr in range(node_level + 1):
            m = self.params.m0 if lyr == 0 else self.params.m
            neighbours = self._graph[lyr].pop(node, [])
            # Repair: connect each live in-neighbour to the node's other live
            # neighbours so deleting an articulation point can't sever the graph.
            live = [n for n in neighbours if n not in self._deleted]
            for owner in live:
                adj = self._graph[lyr].get(owner)
                if adj is None:
                    continue
                if node in adj:
                    adj.remove(node)
                for cand in live:
                    if cand != owner and cand not in adj:
                        adj.append(cand)
                if len(adj) > m:
                    owner_vec = self._vectors[owner]
                    cands = [
                        (float(dist.order_value(owner_vec, self._vectors[n], self.metric)), n)
                        for n in adj
                    ]
                    self._graph[lyr][owner] = self._select_neighbours_heuristic(owner_vec, cands, m)
        if self._entry == node:
            self._entry = self._pick_new_entry()
        return True

    def _pick_new_entry(self) -> int | None:
        # Highest-layer live node becomes the new entry point.
        best: int | None = None
        best_level = -1
        for n, _ in self._node_to_ext.items():
            if n in self._deleted:
                continue
            if self._levels[n] > best_level:
                best_level = self._levels[n]
                best = n
        return best

    @property
    def num_deleted(self) -> int:
        return len(self._deleted)

    # -- compaction --------------------------------------------------------- #
    def compact(self) -> HnswIndex:
        """Rebuild a fresh index from the live nodes only (reclaims tombstones).

        Returns a new :class:`HnswIndex` with the same params/metric — the caller
        swaps it in. Rebuild is the robust way to reclaim deleted slots without
        renumbering a live graph in place.
        """
        rebuilt = HnswIndex(
            self.dim,
            metric=self.metric,
            params=HnswParams(
                m=self.params.m,
                ef_construction=self.params.ef_construction,
                ef_search=self.params.ef_search,
                ml=self.params.ml,
                seed=self.params.seed,
            ),
            capacity=max(self._size, 1),
        )
        for node, vid in self._node_to_ext.items():
            if node in self._deleted:
                continue
            rebuilt.add(vid, self._vectors[node], metadata=self._meta.get(node))
        return rebuilt

    # -- introspection / persistence support -------------------------------- #
    def get_vector(self, vid: VectorId) -> NDArray[np.float32] | None:
        node = self._ext_to_node.get(vid)
        if node is None or node in self._deleted:
            return None
        return self._vectors[node].copy()

    def stats(self) -> HnswStats:
        degrees = [len(adj) for adj in self._graph[0].values()]
        return HnswStats(
            live=self._size,
            deleted=len(self._deleted),
            levels=len(self._graph),
            entry=self._entry,
            avg_degree_l0=(sum(degrees) / len(degrees)) if degrees else 0.0,
            params=self.params,
        )

    # Raw accessors for the storage layer (kept private-ish, no copy).
    def _export(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "metric": self.metric.value,
            "params": {
                "m": self.params.m,
                "ef_construction": self.params.ef_construction,
                "ef_search": self.params.ef_search,
                "ml": self.params.ml,
                "seed": self.params.seed,
            },
            "n_nodes": len(self._levels),
            "levels": list(self._levels),
            "graph": [{str(k): list(v) for k, v in layer.items()} for layer in self._graph],
            "entry": self._entry,
            "deleted": sorted(self._deleted),
            "ext_to_node": dict(self._ext_to_node),
            "meta": {str(k): v for k, v in self._meta.items()},
            "size": self._size,
        }

    @property
    def _vector_view(self) -> NDArray[np.float32]:
        """The dense used portion of the vector matrix (for persistence)."""
        return self._vectors[: len(self._levels)]


@dataclass(frozen=True, slots=True)
class HnswStats:
    """A lightweight snapshot of index health (for dashboards / tests)."""

    live: int
    deleted: int
    levels: int
    entry: int | None
    avg_degree_l0: float
    params: HnswParams = field(compare=False)


__all__ = ["HnswIndex", "HnswParams", "HnswStats"]
