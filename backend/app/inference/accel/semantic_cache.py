"""Semantic response cache — embedding-similarity lookup + exact-prefix cache.

Two cache layers, checked in order:

1. **Exact cache.** A hash of the normalized request (messages + model +
   generation params). A re-read of the identical prompt is a free, certain hit.
2. **Semantic cache.** The prompt is embedded; the nearest stored entry by
   cosine similarity is returned *only* if the similarity clears a calibrated
   threshold (see :mod:`app.inference.accel.calibration`). This catches
   paraphrases / whitespace / trivially reworded prompts that the exact cache
   misses.

Correctness guards that keep a semantic cache honest:

* **Versioning / staleness.** Every entry carries a ``namespace`` *version* tag
  and a ``created_at`` wall time. Bumping the namespace version (e.g. the canon
  changed, §8.7 / §12.3 "entity + version") invalidates all of its entries
  without scanning. A per-entry TTL expires stale answers.
* **Threshold = the safety dial.** Below it, a candidate is a *near-miss* (logged
  in metrics) and the lookup misses rather than returning a possibly-wrong
  answer.
* **Bounded size, LRU eviction.** A capacity cap with least-recently-used
  eviction keeps memory bounded; semantic search is linear over live entries
  (fine for the in-process cache sizes Kinora uses; the interface is ready for a
  pgvector/ANN backend behind the same API).

Determinism: an injected :class:`~app.inference.accel.clock.Clock` drives all
TTL/staleness decisions; the embedder is injected; nothing here sleeps or calls
the network.
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from .clock import SYSTEM_CLOCK, Clock
from .metrics import CacheMetrics
from .protocol import Embedder, GenerationRequest, GenerationResult


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity of two equal-length vectors (0 if either is degenerate)."""
    if len(a) != len(b):
        raise ValueError(f"embedding length mismatch: {len(a)} != {len(b)}")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def exact_key(request: GenerationRequest) -> str:
    """Stable hash over the request's cache-relevant fields."""
    parts = [
        request.model,
        f"t={request.temperature:.6g}",
        f"m={request.max_tokens}",
    ]
    parts.extend(f"{role}\x1f{content}" for role, content in request.messages)
    blob = "\x1e".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """Tunables for the semantic cache."""

    #: Cosine similarity at/above which a semantic neighbour is a hit.
    similarity_threshold: float = 0.92
    #: A neighbour within this margin *below* the threshold is recorded as a
    #: "near-miss" (useful telemetry for re-calibration); still a miss.
    near_miss_margin: float = 0.05
    #: Max live entries before LRU eviction.
    max_entries: int = 1024
    #: Per-entry time-to-live in seconds (None = no TTL).
    ttl_s: float | None = None
    #: Cache only deterministic-enough generations (low temperature). Above this
    #: temperature a store is skipped (sampling makes cached reuse unsound).
    max_cacheable_temperature: float = 0.3


@dataclass(slots=True)
class CacheEntry:
    """A stored answer plus its lookup keys and freshness metadata."""

    exact_key: str
    embedding: tuple[float, ...]
    result: GenerationResult
    namespace: str
    version: int
    created_at: float
    prompt_text: str
    hits: int = 0
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LookupOutcome:
    """The result of a cache lookup."""

    result: GenerationResult | None
    kind: str  # "exact" | "semantic" | "miss"
    similarity: float | None = None

    @property
    def hit(self) -> bool:
        return self.result is not None


class SemanticCache:
    """In-process exact + semantic response cache with calibrated thresholds.

    Keyed within a ``namespace`` (e.g. ``"adapter:book-42"``). Each namespace has
    an integer *version*; bumping it makes every prior entry stale at O(1) cost.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        config: CacheConfig | None = None,
        metrics: CacheMetrics | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._embedder = embedder
        self._config = config or CacheConfig()
        self._metrics = metrics or CacheMetrics()
        self._clock = clock
        # exact_key -> entry, ordered for LRU (most-recently-used at the end).
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        # namespace -> current version.
        self._versions: dict[str, int] = {}

    @property
    def metrics(self) -> CacheMetrics:
        return self._metrics

    @property
    def config(self) -> CacheConfig:
        return self._config

    def set_threshold(self, threshold: float) -> None:
        """Apply a (re)calibrated similarity threshold at runtime."""
        self._config = replace(self._config, similarity_threshold=threshold)

    def size(self) -> int:
        return len(self._entries)

    # -- versioning ------------------------------------------------------- #

    def current_version(self, namespace: str) -> int:
        return self._versions.get(namespace, 0)

    def bump_version(self, namespace: str) -> int:
        """Invalidate every entry in ``namespace`` (canon changed, §12.3)."""
        v = self._versions.get(namespace, 0) + 1
        self._versions[namespace] = v
        return v

    def invalidate_namespace(self, namespace: str) -> int:
        """Drop all live entries for ``namespace`` immediately; return count."""
        keys = [k for k, e in self._entries.items() if e.namespace == namespace]
        for k in keys:
            del self._entries[k]
        return len(keys)

    # -- staleness -------------------------------------------------------- #

    def _is_stale(self, entry: CacheEntry) -> bool:
        if entry.version != self.current_version(entry.namespace):
            return True
        ttl = self._config.ttl_s
        return ttl is not None and (self._clock.time() - entry.created_at) > ttl

    def _purge_stale(self) -> int:
        stale = [k for k, e in self._entries.items() if self._is_stale(e)]
        for k in stale:
            del self._entries[k]
        if stale:
            self._metrics.record_stale_eviction(len(stale))
        return len(stale)

    # -- lookup ----------------------------------------------------------- #

    async def lookup(
        self, request: GenerationRequest, *, namespace: str = "default"
    ) -> LookupOutcome:
        """Look up ``request`` in ``namespace`` (exact first, then semantic)."""
        self._purge_stale()
        key = exact_key(request)

        entry = self._entries.get(key)
        if entry is not None and entry.namespace == namespace and not self._is_stale(entry):
            self._entries.move_to_end(key)
            entry.hits += 1
            self._metrics.record_lookup(exact_hit=True)
            return LookupOutcome(
                result=entry.result.with_meta(cache="exact"), kind="exact", similarity=1.0
            )

        # Semantic search over live entries in this namespace.
        query_vec = await self._embedder.embed(request.prompt_text)
        best: CacheEntry | None = None
        best_sim = -1.0
        for e in self._entries.values():
            if e.namespace != namespace or self._is_stale(e):
                continue
            sim = cosine(query_vec, e.embedding)
            if sim > best_sim:
                best_sim, best = sim, e

        threshold = self._config.similarity_threshold
        if best is not None and best_sim >= threshold:
            self._entries.move_to_end(best.exact_key)
            best.hits += 1
            self._metrics.record_lookup(semantic_hit=True)
            return LookupOutcome(
                result=best.result.with_meta(cache="semantic", similarity=round(best_sim, 6)),
                kind="semantic",
                similarity=best_sim,
            )

        near_miss = best is not None and best_sim >= (threshold - self._config.near_miss_margin)
        self._metrics.record_lookup(near_miss=near_miss)
        return LookupOutcome(result=None, kind="miss", similarity=best_sim if best else None)

    # -- store ------------------------------------------------------------ #

    async def store(
        self,
        request: GenerationRequest,
        result: GenerationResult,
        *,
        namespace: str = "default",
    ) -> bool:
        """Cache ``result`` for ``request`` in ``namespace``. Returns stored?.

        Skips storing when the request's temperature exceeds the cacheable bound
        (sampled outputs are not safely reusable).
        """
        if request.temperature > self._config.max_cacheable_temperature:
            return False
        embedding = await self._embedder.embed(request.prompt_text)
        key = exact_key(request)
        entry = CacheEntry(
            exact_key=key,
            embedding=embedding,
            result=result,
            namespace=namespace,
            version=self.current_version(namespace),
            created_at=self._clock.time(),
            prompt_text=request.prompt_text,
        )
        self._entries[key] = entry
        self._entries.move_to_end(key)
        self._metrics.record_store()
        self._evict_if_needed()
        return True

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._config.max_entries:
            # popitem(last=False) removes the least-recently-used.
            self._entries.popitem(last=False)

    # -- get-or-compute --------------------------------------------------- #

    async def get_or_compute(
        self,
        request: GenerationRequest,
        compute: Callable[[GenerationRequest], Awaitable[GenerationResult]],
        *,
        namespace: str = "default",
    ) -> GenerationResult:
        """Return a cached answer or compute, store, and return a fresh one.

        The single execution path callers use: read-through with write-on-miss.
        """
        outcome = await self.lookup(request, namespace=namespace)
        if outcome.result is not None:
            return outcome.result
        fresh = await compute(request)
        await self.store(request, fresh, namespace=namespace)
        return fresh.with_meta(cache="miss")


__all__ = [
    "CacheConfig",
    "CacheEntry",
    "LookupOutcome",
    "SemanticCache",
    "cosine",
    "exact_key",
]
