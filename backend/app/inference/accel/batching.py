"""Request coalescing + micro-batching.

Two independent savings mechanisms that ride on top of any
:class:`~app.inference.accel.protocol.InferenceBackend`:

* **Single-flight coalescing** (:class:`RequestCoalescer`). When two call sites
  ask for the *same* generation concurrently (the §12.3 "request-level dedup"
  case — two reading sessions hitting the identical prompt), only **one**
  backend call is made and both awaiters share its result. This is distinct from
  the semantic cache: it deduplicates *in-flight* work, so it helps even on the
  very first (uncached) occurrence and needs no stored entry.

* **Micro-batching** (:class:`MicroBatcher`). For a backend that exposes a
  cheaper *batch* path (the §11 Batch API at ~50% off), this accumulates
  individual requests for a short window / up to a max batch size, submits them
  as one batch, and fans the results back to each waiter. Latency is traded for
  cost; the window is bounded so no request waits unboundedly.

Both are deterministic under an injected clock/flush signal — the tests drive
the batch flush explicitly rather than sleeping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .protocol import GenerationRequest, GenerationResult
from .semantic_cache import exact_key

GenerateFn = Callable[[GenerationRequest], Awaitable[GenerationResult]]
BatchFn = Callable[[Sequence[GenerationRequest]], Awaitable[Sequence[GenerationResult]]]


@dataclass(slots=True)
class CoalescerStats:
    requests: int = 0
    backend_calls: int = 0
    coalesced: int = 0

    @property
    def coalesce_rate(self) -> float:
        return self.coalesced / self.requests if self.requests else 0.0


class RequestCoalescer:
    """Collapses concurrent identical generations into a single backend call.

    Keyed by :func:`exact_key`. While a request is in flight, later identical
    requests await the same future instead of issuing their own call. Once the
    call resolves the entry is cleared, so a *subsequent* (non-concurrent)
    identical request issues a fresh call (caching is the cache's job, not this).
    """

    def __init__(self, generate: GenerateFn) -> None:
        self._generate = generate
        self._inflight: dict[str, asyncio.Future[GenerationResult]] = {}
        self._stats = CoalescerStats()

    @property
    def stats(self) -> CoalescerStats:
        return self._stats

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        key = exact_key(request)
        self._stats.requests += 1
        existing = self._inflight.get(key)
        if existing is not None:
            self._stats.coalesced += 1
            return await existing

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[GenerationResult] = loop.create_future()
        self._inflight[key] = fut
        self._stats.backend_calls += 1
        try:
            result = await self._generate(request)
        except BaseException as exc:  # noqa: BLE001 - propagate to all awaiters
            self._inflight.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            raise
        self._inflight.pop(key, None)
        if not fut.done():
            fut.set_result(result)
        return result


@dataclass(slots=True)
class _Pending:
    request: GenerationRequest
    future: asyncio.Future[GenerationResult]


@dataclass(slots=True)
class BatcherStats:
    requests: int = 0
    batches: int = 0
    largest_batch: int = 0

    @property
    def mean_batch_size(self) -> float:
        return self.requests / self.batches if self.batches else 0.0


class MicroBatcher:
    """Accumulates requests and submits them via a batch backend on flush.

    Flushing happens when either the buffer reaches ``max_batch`` *or*
    :meth:`flush` is called explicitly (a scheduler / timer in production calls
    flush every ``window``; tests call it directly for determinism). Each waiter
    gets its own result by position.
    """

    def __init__(self, batch_generate: BatchFn, *, max_batch: int = 16) -> None:
        if max_batch < 1:
            raise ValueError("max_batch must be >= 1")
        self._batch_generate = batch_generate
        self._max_batch = max_batch
        self._pending: list[_Pending] = []
        self._stats = BatcherStats()
        self._lock = asyncio.Lock()

    @property
    def stats(self) -> BatcherStats:
        return self._stats

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def submit(self, request: GenerationRequest) -> GenerationResult:
        """Enqueue ``request``; resolves when its batch flushes."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[GenerationResult] = loop.create_future()
        self._pending.append(_Pending(request=request, future=fut))
        self._stats.requests += 1
        if len(self._pending) >= self._max_batch:
            await self.flush()
        return await fut

    async def flush(self) -> int:
        """Submit all buffered requests as one batch; return the batch size."""
        async with self._lock:
            if not self._pending:
                return 0
            batch = self._pending
            self._pending = []
        requests = [p.request for p in batch]
        self._stats.batches += 1
        self._stats.largest_batch = max(self._stats.largest_batch, len(batch))
        try:
            results = await self._batch_generate(requests)
        except BaseException as exc:  # noqa: BLE001 - fail every waiter in the batch
            for p in batch:
                if not p.future.done():
                    p.future.set_exception(exc)
            raise
        if len(results) != len(batch):
            err = ValueError(
                f"batch backend returned {len(results)} results for {len(batch)} requests"
            )
            for p in batch:
                if not p.future.done():
                    p.future.set_exception(err)
            raise err
        for p, res in zip(batch, results, strict=True):
            if not p.future.done():
                p.future.set_result(res)
        return len(batch)


def batch_from_single(generate: GenerateFn) -> BatchFn:
    """Adapt a single-request backend into a batch backend (sequential map).

    Useful when a backend has no native batch path but a caller still wants the
    :class:`MicroBatcher` API; the cost saving then comes only from any upstream
    coalescing, but the call shape is uniform.
    """

    async def batch(requests: Sequence[GenerationRequest]) -> list[GenerationResult]:
        return [await generate(r) for r in requests]

    return batch


__all__ = [
    "BatchFn",
    "BatcherStats",
    "CoalescerStats",
    "GenerateFn",
    "MicroBatcher",
    "RequestCoalescer",
    "batch_from_single",
]
