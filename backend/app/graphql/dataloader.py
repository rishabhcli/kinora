"""A per-request DataLoader: batch + cache to eliminate N+1 resolver fan-out.

A field resolved once per parent (e.g. ``Shot.book``) would otherwise issue one
DB round-trip per item. :class:`DataLoader` collects the keys requested within a
single event-loop tick, dispatches one batch call, and caches results for the
life of the request so repeated keys are free.

The loader is intentionally tied to a request (constructed fresh per execution
in ``app/graphql/context.py``); it is not a process-global cache, so it never
serves stale rows across requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable, Sequence
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

#: A batch function maps a list of keys to a list of results, **positionally**
#: aligned (result[i] is the value for keys[i]; use ``None`` for a miss).
BatchFn = Callable[[Sequence[K]], Awaitable[Sequence[V | None]]]


class DataLoader(Generic[K, V]):
    """Batches ``load`` calls made within one tick into a single ``batch_fn``."""

    def __init__(self, batch_fn: BatchFn[K, V], *, max_batch_size: int = 256) -> None:
        self._batch_fn = batch_fn
        self._max_batch_size = max_batch_size
        self._cache: dict[K, V | None] = {}
        self._queue: list[tuple[K, asyncio.Future[V | None]]] = []
        self._dispatch_scheduled = False

    def load(self, key: K) -> Awaitable[V | None]:
        """Return an awaitable for ``key`` (resolved by the next batch dispatch)."""
        if key in self._cache:
            fut: asyncio.Future[V | None] = asyncio.get_event_loop().create_future()
            fut.set_result(self._cache[key])
            return fut
        future: asyncio.Future[V | None] = asyncio.get_event_loop().create_future()
        self._queue.append((key, future))
        if not self._dispatch_scheduled:
            self._dispatch_scheduled = True
            asyncio.get_event_loop().call_soon(self._schedule_dispatch)
        return future

    async def load_many(self, keys: Sequence[K]) -> list[V | None]:
        """Load several keys, preserving order."""
        return list(await asyncio.gather(*[self.load(k) for k in keys]))

    def prime(self, key: K, value: V | None) -> None:
        """Seed the cache so a later ``load(key)`` is free (e.g. after a list read)."""
        self._cache.setdefault(key, value)

    def clear(self, key: K) -> None:
        self._cache.pop(key, None)

    def _schedule_dispatch(self) -> None:
        self._dispatch_scheduled = False
        if self._queue:
            asyncio.ensure_future(self._dispatch())

    async def _dispatch(self) -> None:
        # Drain the queue; de-duplicate keys so the batch_fn sees each key once.
        batch = self._queue
        self._queue = []
        # Chunk to the max batch size.
        for chunk_start in range(0, len(batch), self._max_batch_size):
            chunk = batch[chunk_start : chunk_start + self._max_batch_size]
            await self._dispatch_chunk(chunk)

    async def _dispatch_chunk(self, chunk: list[tuple[K, asyncio.Future[V | None]]]) -> None:
        unique_keys: list[K] = []
        seen: set[K] = set()
        for key, _ in chunk:
            if key not in seen:
                seen.add(key)
                unique_keys.append(key)
        try:
            results = await self._batch_fn(unique_keys)
        except Exception as exc:  # noqa: BLE001 - propagate to every awaiting future
            for _, future in chunk:
                if not future.done():
                    future.set_exception(exc)
            return
        by_key = dict(zip(unique_keys, results, strict=False))
        for key in unique_keys:
            self._cache.setdefault(key, by_key.get(key))
        for key, future in chunk:
            if not future.done():
                future.set_result(by_key.get(key))


class DataLoaderRegistry:
    """A per-request bag of named loaders, built lazily from factory functions."""

    def __init__(self) -> None:
        self._loaders: dict[str, DataLoader[object, object]] = {}
        self._factories: dict[str, Callable[[], DataLoader[object, object]]] = {}

    def register(self, name: str, factory: Callable[[], DataLoader[object, object]]) -> None:
        self._factories[name] = factory

    def get(self, name: str) -> DataLoader[object, object]:
        if name not in self._loaders:
            if name not in self._factories:
                raise KeyError(f"no dataloader registered under {name!r}")
            self._loaders[name] = self._factories[name]()
        return self._loaders[name]


__all__ = ["BatchFn", "DataLoader", "DataLoaderRegistry"]
