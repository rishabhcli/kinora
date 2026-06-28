"""Response cache keyed by prompt-version + inputs.

A deterministic agent call (same system prompt revision, same inputs, same
temperature) can be served from cache instead of re-spending tokens — exactly the
kind of saving the §11 budget cares about. This module is the cache:

* :func:`cache_key` — a stable sha256 over ``(prompt_key, prompt_version, model,
  canonical(inputs), temperature)``. Inputs are canonicalized (sorted keys,
  stable separators) so equivalent dicts collide; floats are rounded so harmless
  representational noise doesn't miss.
* :class:`ResponseCache` — an in-memory **TTL + LRU-bounded** cache with hit/miss
  accounting. ``get_or_set`` runs the (async) producer on a miss. A pluggable
  backend protocol (:class:`CacheBackend`) lets a Redis-backed implementation slot
  in later without changing call sites; :class:`InMemoryBackend` is the default.

Caching is **opt-in per call** (the caller passes ``temperature`` and decides
whether a call is cacheable) so it never silently serves a stale answer for a
creative, high-temperature generation. Pure + deterministic; no app imports.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


def _canonical(value: Any) -> Any:
    """Canonicalize a value for stable hashing (sorted dicts, rounded floats)."""
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def cache_key(
    *,
    prompt_key: str,
    prompt_version: str,
    model: str,
    inputs: dict[str, Any],
    temperature: float | None = None,
) -> str:
    """A stable cache key over the call's identity."""
    payload = {
        "prompt_key": prompt_key,
        "prompt_version": prompt_version,
        "model": model,
        "inputs": _canonical(inputs),
        "temperature": round(temperature, 6) if temperature is not None else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "llmops:cache:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    value: str
    expires_at: float | None  # monotonic deadline; None = no expiry


class CacheBackend(Protocol):
    """Storage behind the cache (memory now, Redis-ready)."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_s: float | None) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...

    def __len__(self) -> int: ...


@dataclass
class InMemoryBackend:
    """An LRU + TTL in-memory backend (the default)."""

    max_entries: int = 2048
    _store: OrderedDict[str, _Entry] = field(default_factory=OrderedDict)
    _clock: Callable[[], float] = time.monotonic

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and self._clock() >= entry.expires_at:
            del self._store[key]
            return None
        self._store.move_to_end(key)  # LRU touch
        return entry.value

    def set(self, key: str, value: str, ttl_s: float | None) -> None:
        deadline = self._clock() + ttl_s if ttl_s is not None else None
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = _Entry(value=value, expires_at=deadline)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)  # evict LRU

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    sets: int
    size: int

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return round(self.hits / self.total, 6) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "size": self.size,
            "total": self.total,
            "hit_rate": self.hit_rate,
        }


@dataclass
class ResponseCache:
    """Prompt-version + inputs keyed response cache with hit/miss accounting."""

    backend: CacheBackend = field(default_factory=InMemoryBackend)
    default_ttl_s: float | None = 3600.0
    _hits: int = 0
    _misses: int = 0
    _sets: int = 0

    def get(
        self,
        *,
        prompt_key: str,
        prompt_version: str,
        model: str,
        inputs: dict[str, Any],
        temperature: float | None = None,
    ) -> str | None:
        key = cache_key(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            model=model,
            inputs=inputs,
            temperature=temperature,
        )
        value = self.backend.get(key)
        if value is None:
            self._misses += 1
        else:
            self._hits += 1
        return value

    def set(
        self,
        value: str,
        *,
        prompt_key: str,
        prompt_version: str,
        model: str,
        inputs: dict[str, Any],
        temperature: float | None = None,
        ttl_s: float | None = ...,  # type: ignore[assignment]
    ) -> None:
        key = cache_key(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            model=model,
            inputs=inputs,
            temperature=temperature,
        )
        ttl = self.default_ttl_s if ttl_s is ... else ttl_s
        self.backend.set(key, value, ttl)
        self._sets += 1

    async def get_or_set(
        self,
        producer: Callable[[], Awaitable[str]],
        *,
        prompt_key: str,
        prompt_version: str,
        model: str,
        inputs: dict[str, Any],
        temperature: float | None = None,
        ttl_s: float | None = ...,  # type: ignore[assignment]
    ) -> tuple[str, bool]:
        """Return ``(value, cache_hit)``; runs ``producer`` only on a miss."""
        cached = self.get(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            model=model,
            inputs=inputs,
            temperature=temperature,
        )
        if cached is not None:
            return cached, True
        value = await producer()
        self.set(
            value,
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            model=model,
            inputs=inputs,
            temperature=temperature,
            ttl_s=ttl_s,
        )
        return value, False

    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits, misses=self._misses, sets=self._sets, size=len(self.backend)
        )

    def clear(self) -> None:
        self.backend.clear()
        self._hits = self._misses = self._sets = 0


__all__ = [
    "CacheBackend",
    "CacheStats",
    "InMemoryBackend",
    "ResponseCache",
    "cache_key",
]
