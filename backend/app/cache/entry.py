"""The cache entry value object — value + expiry + provenance metadata.

A :class:`CacheEntry` is what every backend stores. It carries:

* the user ``value`` (or a negative-cache marker),
* an absolute ``expires_at`` wall timestamp (``None`` = never),
* ``created_at`` and a ``ttl`` snapshot used by probabilistic early expiry
  (§ XFetch: the older the entry, the more eager a single reader is to refresh
  it *before* it expires, which spreads stampedes out over time),
* the set of ``tags`` the entry belongs to (for tag-based invalidation), and
* a ``negative`` flag distinguishing "we cached the absence of a value" from
  "we cached ``None`` as a real value".

Entries are immutable once built (``frozen``); a refresh produces a new entry.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any

#: Sentinel marking a negative cache entry's value slot (never user-visible).
_NEGATIVE = object()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """An immutable cached value with TTL and invalidation metadata."""

    value: Any
    created_at: float
    expires_at: float | None = None
    #: The TTL the entry was created with (seconds); drives early-expiry math.
    ttl: float | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    negative: bool = False
    #: Optional codec name the value was stored under (informational).
    codec: str | None = None

    @classmethod
    def of(
        cls,
        value: Any,
        *,
        now: float,
        ttl: float | None = None,
        tags: frozenset[str] | None = None,
        negative: bool = False,
        codec: str | None = None,
    ) -> CacheEntry:
        """Build an entry expiring ``ttl`` seconds after ``now`` (None TTL = never)."""
        expires_at = None if ttl is None else now + ttl
        return cls(
            value=_NEGATIVE if negative else value,
            created_at=now,
            expires_at=expires_at,
            ttl=ttl,
            tags=tags or frozenset(),
            negative=negative,
            codec=codec,
        )

    def is_expired(self, now: float) -> bool:
        """Whether the entry's hard TTL has elapsed by ``now``."""
        return self.expires_at is not None and now >= self.expires_at

    def remaining(self, now: float) -> float:
        """Seconds until hard expiry (``inf`` if no TTL, ``0`` if already expired)."""
        if self.expires_at is None:
            return math.inf
        return max(0.0, self.expires_at - now)

    def age(self, now: float) -> float:
        """Seconds since the entry was created."""
        return max(0.0, now - self.created_at)

    def should_early_expire(
        self,
        now: float,
        *,
        beta: float = 1.0,
        delta: float | None = None,
        rng: random.Random | None = None,
    ) -> bool:
        """Probabilistic early recompute (XFetch / "early expiration").

        Returns True when a single reader should *voluntarily* treat a still-valid
        entry as stale and recompute it, so the population refreshes the key
        gradually instead of every reader stampeding at the exact expiry instant.

        The XFetch rule recomputes when::

            now - delta * beta * ln(rand()) >= expires_at

        where ``delta`` estimates the recompute cost (defaults to a fraction of
        the TTL) and ``beta`` tunes aggressiveness (larger = earlier). Entries
        with no TTL never early-expire.
        """
        if self.expires_at is None or self.ttl is None:
            return False
        if now >= self.expires_at:
            return True
        gen = rng or random
        # delta defaults to ~1% of the TTL: a cheap proxy for "recompute cost".
        d = delta if delta is not None else max(self.ttl * 0.01, 1e-3)
        # ln(rand()) is negative; subtracting widens the window as the entry ages.
        jitter = d * beta * -math.log(max(gen.random(), 1e-12))
        return (now + jitter) >= self.expires_at

    def with_value(self, value: Any) -> CacheEntry:
        """Return a copy with a replaced value (keeps timing/tags)."""
        return replace(self, value=value, negative=False)


__all__ = ["CacheEntry"]
