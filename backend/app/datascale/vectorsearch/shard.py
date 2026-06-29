"""A sharded ANN index: a router fans a vector to one shard, queries fan out.

Horizontal scale-out: no single HNSW graph holds everything. Writes are routed
to exactly one shard by a :class:`Router` (so an id lives in one place and
delete/update are unambiguous); queries fan out to **all** shards (each returns
its local top-``k``) and a k-way **merge** produces the global top-``k``. Because
each shard answers with the same "smaller is closer" ordering key, the merge is
a correct global ordering — sharding never changes which neighbours win, only
how the work is split.

Routers
-------
* :class:`HashRouter` — stable hash of the id modulo ``n_shards`` (default;
  even load, no data skew assumptions).
* :class:`ModuloRouter` — same idea, injectable hash for tests.
* :class:`AttributeRouter` — route by a metadata field (e.g. ``book_id``) so a
  filtered query can be narrowed to the shards that can possibly match.

The shard index is metric- and param-homogeneous; each shard is a full
:class:`HnswIndex`, so persistence, deletion and stats all compose.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import storage
from .hnsw import HnswIndex, HnswParams
from .merge import merge_results
from .types import Metadata, Metric, SearchResult, VectorId


class Router(ABC):
    """Maps an external id (and optional metadata) to a shard index."""

    def __init__(self, n_shards: int) -> None:
        if n_shards < 1:
            raise ValueError("n_shards must be >= 1")
        self.n_shards = n_shards

    @abstractmethod
    def route(self, vid: VectorId, metadata: Metadata | None = None) -> int:
        """Return the shard index ``[0, n_shards)`` that owns ``vid``."""

    def query_shards(self, where: Mapping[str, Any] | None) -> list[int]:
        """Shards a query must visit. Default: all of them (fan-out)."""
        return list(range(self.n_shards))


def _stable_hash(s: str) -> int:
    """A process-stable 64-bit hash (``hash()`` is salted per-process)."""
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


class HashRouter(Router):
    """Stable-hash sharding: ``blake2b(id) % n_shards``."""

    def route(self, vid: VectorId, metadata: Metadata | None = None) -> int:
        return _stable_hash(vid) % self.n_shards


class ModuloRouter(Router):
    """Sharding by an injectable hash function (deterministic, test-friendly)."""

    def __init__(self, n_shards: int, hash_fn: Any = _stable_hash) -> None:
        super().__init__(n_shards)
        self._hash = hash_fn

    def route(self, vid: VectorId, metadata: Metadata | None = None) -> int:
        return int(self._hash(vid)) % self.n_shards


class AttributeRouter(Router):
    """Route by a metadata field's value, so queries can prune shards.

    A vector with no value for ``field`` falls back to id-hash routing. A query
    whose ``where`` pins the field to a single equality value visits only that
    shard — turning a metadata filter into a shard prune (pre-filtering at the
    routing layer).
    """

    def __init__(self, n_shards: int, field: str) -> None:
        super().__init__(n_shards)
        self.field = field

    def route(self, vid: VectorId, metadata: Metadata | None = None) -> int:
        if metadata is not None and self.field in metadata:
            return _stable_hash(str(metadata[self.field])) % self.n_shards
        return _stable_hash(vid) % self.n_shards

    def query_shards(self, where: Mapping[str, Any] | None) -> list[int]:
        if where:
            val = where.get(self.field)
            if isinstance(val, (str, int, float, bool)):
                return [_stable_hash(str(val)) % self.n_shards]
            if isinstance(val, Mapping) and "$eq" in val:
                return [_stable_hash(str(val["$eq"])) % self.n_shards]
        return list(range(self.n_shards))


class ShardedIndex:
    """An ANN index partitioned across ``n_shards`` HNSW graphs."""

    def __init__(
        self,
        dim: int,
        *,
        n_shards: int = 4,
        metric: Metric = Metric.COSINE,
        params: HnswParams | None = None,
        router: Router | None = None,
    ) -> None:
        if n_shards < 1:
            raise ValueError("n_shards must be >= 1")
        self.dim = dim
        self.metric = metric
        self.n_shards = n_shards
        self.router = router or HashRouter(n_shards)
        if self.router.n_shards != n_shards:
            raise ValueError("router.n_shards must match n_shards")
        base = params or HnswParams()
        # Give each shard its own seed offset for independent (still deterministic) layers.
        self._shards: list[HnswIndex] = [
            HnswIndex(
                dim,
                metric=metric,
                params=HnswParams(
                    m=base.m,
                    ef_construction=base.ef_construction,
                    ef_search=base.ef_search,
                    ml=base.ml,
                    seed=base.seed + i,
                ),
            )
            for i in range(n_shards)
        ]
        self._owner: dict[VectorId, int] = {}

    # -- mutation ----------------------------------------------------------- #
    def add(self, vid: VectorId, vector: Any, *, metadata: Metadata | None = None) -> int:
        """Route + insert; returns the owning shard. Re-add stays on its shard."""
        shard = self._owner.get(vid)
        if shard is None:
            shard = self.router.route(vid, metadata)
            self._owner[vid] = shard
        self._shards[shard].add(vid, vector, metadata=metadata)
        return shard

    def add_many(
        self,
        ids: Sequence[VectorId],
        vectors: Any,
        *,
        metadatas: Sequence[Metadata | None] | None = None,
    ) -> None:
        metas = metadatas or [None] * len(ids)
        for vid, vec, meta in zip(ids, vectors, metas, strict=True):
            self.add(vid, vec, metadata=meta)

    def remove(self, vid: VectorId) -> bool:
        shard = self._owner.pop(vid, None)
        if shard is None:
            return False
        return self._shards[shard].remove(vid)

    # -- query -------------------------------------------------------------- #
    def search(
        self,
        vector: Any,
        k: int = 10,
        *,
        ef: int | None = None,
        where: Mapping[str, Any] | None = None,
        per_shard_k: int | None = None,
    ) -> list[SearchResult]:
        """Fan out to the relevant shards and merge their local top-``k``.

        ``per_shard_k`` (default ``k``) is how many each shard returns before the
        merge — raising it improves global recall when results cluster on one
        shard, at the cost of more per-shard work.
        """
        if k <= 0:
            return []
        local_k = per_shard_k or k
        targets = self.router.query_shards(where)
        per_shard: list[list[SearchResult]] = []
        for s in targets:
            per_shard.append(self._shards[s].search(vector, local_k, ef=ef, where=where))
        return merge_results(per_shard, k)

    # -- introspection ------------------------------------------------------ #
    def __len__(self) -> int:
        return sum(len(s) for s in self._shards)

    def __contains__(self, vid: object) -> bool:
        if not isinstance(vid, str):
            return False
        shard = self._owner.get(vid)
        return shard is not None and vid in self._shards[shard]

    def shard_sizes(self) -> list[int]:
        return [len(s) for s in self._shards]

    def shard_of(self, vid: VectorId) -> int | None:
        return self._owner.get(vid)

    def ids(self) -> Iterable[VectorId]:
        return tuple(self._owner.keys())

    def compact(self) -> None:
        """Compact every shard in place (reclaims tombstones)."""
        for i, shard in enumerate(self._shards):
            self._shards[i] = shard.compact()

    # -- persistence -------------------------------------------------------- #
    def save(self, path: str | Path) -> Path:
        """Persist each shard into ``path/shard_<i>`` + a manifest."""
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        for i, shard in enumerate(self._shards):
            storage.save_index(shard, out / f"shard_{i}")
        manifest = {
            "dim": self.dim,
            "metric": self.metric.value,
            "n_shards": self.n_shards,
            "router": type(self.router).__name__,
            "router_field": getattr(self.router, "field", None),
            "owner": dict(self._owner),
        }
        import json

        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: str | Path, *, mmap: bool = True) -> ShardedIndex:
        import json

        src = Path(path)
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
        n_shards = int(manifest["n_shards"])
        router_name = manifest.get("router", "HashRouter")
        field = manifest.get("router_field")
        router: Router
        if router_name == "AttributeRouter" and field:
            router = AttributeRouter(n_shards, field)
        else:
            router = HashRouter(n_shards)
        idx = cls(
            int(manifest["dim"]),
            n_shards=n_shards,
            metric=Metric(manifest["metric"]),
            router=router,
        )
        idx._shards = [storage.load_index(src / f"shard_{i}", mmap=mmap) for i in range(n_shards)]
        idx._owner = {str(k): int(v) for k, v in manifest["owner"].items()}
        return idx


def rebalance_plan(sizes: Sequence[int]) -> float:
    """Load-imbalance ratio ``max/mean`` (1.0 = perfectly balanced).

    A diagnostic the ops layer can alarm on; a hot shard above a threshold is the
    signal to bump ``n_shards`` or switch routers.
    """
    if not sizes:
        return 1.0
    mean = sum(sizes) / len(sizes)
    if mean == 0:
        return 1.0
    return max(sizes) / mean


__all__ = [
    "AttributeRouter",
    "HashRouter",
    "ModuloRouter",
    "Router",
    "ShardedIndex",
    "rebalance_plan",
]
