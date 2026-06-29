"""N+1 query detection + an async dataloader / batch-resolver framework.

Two cooperating pieces:

* :class:`NPlusOneDetector` — observes queries within a logical *window* (a
  request, a job, a scheduler tick) and flags the classic N+1 pattern: the same
  parameterised query shape executed many times with different bound parameters,
  i.e. one query per parent row instead of one batched query. Detection is purely
  by fingerprint frequency, so it needs no driver hooks; an instrumented engine or
  a manual ``observe`` call feeds it. It reports the offending shape, the call
  count, and a *severity* so a CI gate can fail a request that fans out beyond a
  threshold.

* :class:`DataLoader` — the cure. A per-key async batching primitive (Facebook's
  DataLoader, adapted to asyncio): callers ``await loader.load(key)`` and within
  one event-loop tick all pending keys are coalesced into a single batch call to a
  user-supplied ``batch_fn``. Duplicate keys in the same tick share one result.
  This turns "N round-trips" into "1 round-trip of N keys" — the structural fix
  the detector points at.

Both are deterministic and infra-free: the detector is fed observations; the
loader's batching boundary is a single ``asyncio`` tick, exercised directly in
tests.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

from app.datascale.optimize.fingerprint import make_fingerprint

K = TypeVar("K", bound=Hashable)
R = TypeVar("R")


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


class Severity(StrEnum):
    """How bad an N+1 finding is."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class NPlusOneFinding:
    """One detected repeated-query pattern within a window."""

    fingerprint: str
    skeleton: str
    count: int
    distinct_params: int
    severity: Severity

    def as_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint[:12],
            "skeleton": self.skeleton,
            "count": self.count,
            "distinct_params": self.distinct_params,
            "severity": str(self.severity),
        }


@dataclass(slots=True)
class _ShapeCounter:
    skeleton: str
    count: int = 0
    param_hashes: set[str] = field(default_factory=set)


class NPlusOneDetector:
    """Accumulates query observations and flags N+1 fan-out patterns.

    ``threshold`` is the minimum repeat count for a shape to be *suspicious*;
    ``distinct_ratio`` is the minimum fraction of calls that must carry distinct
    parameters for the pattern to look like a per-row loop (as opposed to a hot
    query legitimately re-run with the same params, which a cache — not batching —
    would fix). Severity scales with the count above the threshold.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        distinct_ratio: float = 0.5,
        medium_at: int = 20,
        high_at: int = 50,
    ) -> None:
        if threshold < 2:
            raise ValueError("threshold must be >= 2 (one query is never N+1)")
        self._threshold = threshold
        self._distinct_ratio = distinct_ratio
        self._medium_at = medium_at
        self._high_at = high_at
        self._shapes: dict[str, _ShapeCounter] = {}

    def observe(self, sql: str, params: object = None) -> None:
        """Record one executed query within the current window."""
        qf = make_fingerprint(sql)
        counter = self._shapes.get(qf.hexdigest)
        if counter is None:
            counter = _ShapeCounter(skeleton=qf.skeleton)
            self._shapes[qf.hexdigest] = counter
        counter.count += 1
        counter.param_hashes.add(repr(params))

    def _severity(self, count: int) -> Severity:
        if count >= self._high_at:
            return Severity.HIGH
        if count >= self._medium_at:
            return Severity.MEDIUM
        return Severity.LOW

    def findings(self) -> list[NPlusOneFinding]:
        """All detected N+1 patterns in the window, worst-first."""
        out: list[NPlusOneFinding] = []
        for fp, counter in self._shapes.items():
            if counter.count < self._threshold:
                continue
            distinct = len(counter.param_hashes)
            if distinct / counter.count < self._distinct_ratio:
                # Same params repeated → a caching problem, not a batching one.
                continue
            out.append(
                NPlusOneFinding(
                    fingerprint=fp,
                    skeleton=counter.skeleton,
                    count=counter.count,
                    distinct_params=distinct,
                    severity=self._severity(counter.count),
                )
            )
        out.sort(key=lambda f: f.count, reverse=True)
        return out

    def worst_severity(self) -> Severity:
        """The highest severity across all findings (``NONE`` when clean)."""
        findings = self.findings()
        if not findings:
            return Severity.NONE
        order = {Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3}
        return max(findings, key=lambda f: order[f.severity]).severity

    def reset(self) -> None:
        """Clear all observations (call at the start of each window)."""
        self._shapes.clear()


# --------------------------------------------------------------------------- #
# DataLoader (the batching cure)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DataLoaderStats:
    """Counters: how many individual loads coalesced into how many batches."""

    loads: int = 0
    batches: int = 0
    keys_batched: int = 0
    cache_hits: int = 0

    @property
    def coalesce_ratio(self) -> float:
        """Average keys per batch — the N+1 reduction factor."""
        return self.keys_batched / self.batches if self.batches else 0.0


BatchFn = Callable[[Sequence[K]], Awaitable[Sequence[R]]]


class DataLoader(Generic[K, R]):
    """Coalesce per-key async loads in one event-loop tick into one batch call.

    ``batch_fn(keys)`` must return results **positionally aligned** with ``keys``:
    ``result[i]`` is the value for ``keys[i]``. A per-tick cache dedups repeated
    keys; an optional cross-tick ``cache`` keeps results across ticks for the
    loader's lifetime (cleared via :meth:`clear`). Missing keys may be represented
    by whatever sentinel the batch_fn returns (e.g. ``None``) — the loader does not
    interpret values.
    """

    def __init__(
        self,
        batch_fn: BatchFn[K, R],
        *,
        max_batch_size: int | None = None,
        cache: bool = True,
    ) -> None:
        self._batch_fn = batch_fn
        self._max_batch_size = max_batch_size
        self._cache_enabled = cache
        self._cache: dict[K, R] = {}
        # Pending keys for the current tick → list of futures awaiting that key.
        self._queue: dict[K, list[asyncio.Future[R]]] = defaultdict(list)
        self._dispatch_scheduled = False
        self.stats = DataLoaderStats()

    async def load(self, key: K) -> R:
        """Load one key, batched with all other keys requested this tick."""
        self.stats.loads += 1
        if self._cache_enabled and key in self._cache:
            self.stats.cache_hits += 1
            return self._cache[key]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[R] = loop.create_future()
        self._queue[key].append(future)
        if not self._dispatch_scheduled:
            self._dispatch_scheduled = True
            loop.call_soon(self._schedule_dispatch)
        return await future

    async def load_many(self, keys: Iterable[K]) -> list[R]:
        """Load several keys, all coalesced into the same tick's batch."""
        return await asyncio.gather(*(self.load(k) for k in keys))

    def _schedule_dispatch(self) -> None:
        # Kick off the async dispatch from the sync callback scheduled via
        # ``call_soon`` (this is the "tick boundary").
        self._dispatch_scheduled = False
        asyncio.ensure_future(self._dispatch())  # noqa: RUF006 - fire-and-forget by design

    async def _dispatch(self) -> None:
        queue = self._queue
        self._queue = defaultdict(list)
        if not queue:
            return
        keys = list(queue.keys())
        # Honour an optional max batch size by chunking.
        for chunk in self._chunk(keys, self._max_batch_size):
            await self._dispatch_chunk(chunk, queue)

    async def _dispatch_chunk(
        self, chunk: list[K], queue: dict[K, list[asyncio.Future[R]]]
    ) -> None:
        self.stats.batches += 1
        self.stats.keys_batched += len(chunk)
        try:
            results = await self._batch_fn(chunk)
        except Exception as exc:  # noqa: BLE001 - propagate to every awaiting future
            for key in chunk:
                for fut in queue[key]:
                    if not fut.done():
                        fut.set_exception(exc)
            return
        if len(results) != len(chunk):
            err = ValueError(
                f"batch_fn returned {len(results)} results for {len(chunk)} keys"
            )
            for key in chunk:
                for fut in queue[key]:
                    if not fut.done():
                        fut.set_exception(err)
            return
        for key, value in zip(chunk, results, strict=True):
            if self._cache_enabled:
                self._cache[key] = value
            for fut in queue[key]:
                if not fut.done():
                    fut.set_result(value)

    @staticmethod
    def _chunk(keys: list[K], size: int | None) -> list[list[K]]:
        if size is None or size <= 0:
            return [keys]
        return [keys[i : i + size] for i in range(0, len(keys), size)]

    def clear(self, key: K | None = None) -> None:
        """Clear the cross-tick cache (one key, or all when ``key`` is ``None``)."""
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    def prime(self, key: K, value: R) -> None:
        """Seed the cache with a known value (skips a future batch call)."""
        if self._cache_enabled:
            self._cache[key] = value


__all__ = [
    "BatchFn",
    "DataLoader",
    "DataLoaderStats",
    "NPlusOneDetector",
    "NPlusOneFinding",
    "Severity",
]
