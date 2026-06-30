"""Edge-cache policy model over a pluggable CDN provider.

Two asset shapes, two policies:

* **Content-addressed assets are immutable.** A blob keyed by its own sha256
  (``media/by-hash/aa/bb/<sha256>.ext``, the §8.7 byte-dedup layout from
  :mod:`app.media.hashing`) can never change for a given key, so it earns
  ``immutable`` + a one-year TTL and *never* needs purging. This is the fast,
  resilient path for a reader far from origin: edges hold it forever.

* **Mutable / path-keyed assets** (``clips/{book}/{shot}.mp4`` — a shot that may
  be surgically re-rendered after a Director edit, §8.7) get a bounded TTL and
  must be **purged on invalidate** so a re-render is seen promptly. The render
  pipeline / canon-edit path calls :meth:`EdgeCachePolicy.invalidate` with the
  affected key; the policy turns that into ``Cache-Control`` for the response
  *and* a provider purge.

:meth:`cache_control_header` produces the exact header string a serving layer
sets, so the policy is the single source of truth for both the response header
and the purge behaviour. Pure logic except the async purge, which delegates to
the injected :class:`app.cdn.protocols.CdnProvider`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.cdn.errors import CachePurgeError
from app.cdn.protocols import CdnProvider
from app.core.logging import get_logger
from app.media.hashing import CONTENT_ADDRESS_PREFIX

logger = get_logger("app.cdn.cache")

#: One year — the conventional "effectively forever" TTL for immutable assets.
IMMUTABLE_TTL_S = 365 * 24 * 3600

#: Default edge TTL for a mutable, path-keyed asset (a shot that may re-render).
DEFAULT_MUTABLE_TTL_S = 6 * 3600


class CacheClass(StrEnum):
    """How an asset key behaves in the edge cache."""

    #: Content-addressed: bytes are a function of the key; cache forever.
    IMMUTABLE = "immutable"
    #: Path-keyed: may change in place; bounded TTL + purge-on-invalidate.
    MUTABLE = "mutable"


def classify_key(key: str) -> CacheClass:
    """Classify a key as immutable (content-addressed) or mutable (path-keyed)."""
    head = key.lstrip("/")
    if head.startswith(CONTENT_ADDRESS_PREFIX):
        return CacheClass.IMMUTABLE
    return CacheClass.MUTABLE


class CachePolicy(BaseModel):
    """The resolved caching decision for one key."""

    model_config = ConfigDict(frozen=True)

    key: str
    cache_class: CacheClass
    ttl_s: int
    immutable: bool
    #: Whether an invalidate of this key requires a provider purge (mutable only).
    purge_on_invalidate: bool

    def cache_control_header(self) -> str:
        """The ``Cache-Control`` value a serving layer should set for this key.

        Immutable assets get ``public, max-age=<1y>, immutable`` so browsers and
        edges never revalidate; mutable assets get ``public, max-age=<ttl>,
        stale-while-revalidate=<ttl>`` so a re-render is picked up within the TTL
        while still serving instantly from the edge in the meantime.
        """
        if self.immutable:
            return f"public, max-age={self.ttl_s}, immutable"
        return f"public, max-age={self.ttl_s}, stale-while-revalidate={self.ttl_s}"


class EdgeCachePolicy:
    """Resolves cache policy per key and drives purges over a CDN provider.

    Stateless apart from its TTL configuration; the provider is the only I/O.
    """

    def __init__(
        self,
        *,
        immutable_ttl_s: int = IMMUTABLE_TTL_S,
        mutable_ttl_s: int = DEFAULT_MUTABLE_TTL_S,
    ) -> None:
        self._immutable_ttl_s = immutable_ttl_s
        self._mutable_ttl_s = mutable_ttl_s

    def policy_for(self, key: str) -> CachePolicy:
        """Resolve the :class:`CachePolicy` for ``key``."""
        cache_class = classify_key(key)
        if cache_class is CacheClass.IMMUTABLE:
            return CachePolicy(
                key=key,
                cache_class=cache_class,
                ttl_s=self._immutable_ttl_s,
                immutable=True,
                purge_on_invalidate=False,
            )
        return CachePolicy(
            key=key,
            cache_class=cache_class,
            ttl_s=self._mutable_ttl_s,
            immutable=False,
            purge_on_invalidate=True,
        )

    async def invalidate(
        self,
        key: str,
        providers: Iterable[CdnProvider],
    ) -> CachePolicy:
        """Invalidate ``key`` across ``providers`` (purge-on-invalidate).

        Immutable keys are a no-op (their bytes can't change, so a stale edge
        copy is still correct) — purging them would only cost a cold re-fetch.
        Mutable keys are purged from every supplied edge; the first failure is
        surfaced as :class:`CachePurgeError` after attempting the rest so one
        flaky edge can't strand the others.
        """
        policy = self.policy_for(key)
        if not policy.purge_on_invalidate:
            logger.debug("cdn.cache.invalidate.skip_immutable", key=key)
            return policy
        first_error: CachePurgeError | None = None
        purged: list[str] = []
        for provider in providers:
            try:
                await provider.invalidate(key)
                purged.append(provider.region_id)
            except CachePurgeError as exc:
                if first_error is None:
                    first_error = exc
            except Exception as exc:  # noqa: BLE001 - normalise provider failures
                if first_error is None:
                    first_error = CachePurgeError(key, str(exc))
        logger.info("cdn.cache.invalidate", key=key, purged=purged)
        if first_error is not None:
            raise first_error
        return policy


def invalidation_keys_for_shot(book_id: str, shot_id: str) -> Sequence[str]:
    """The mutable edge keys a re-rendered shot invalidates (the clip path).

    Content-addressed derivatives of the shot are immutable and need no purge, so
    only the path-keyed clip (which is overwritten in place by a re-render) is
    returned here.
    """
    from app.storage.object_store import keys as _keys

    return (_keys.clip(book_id, shot_id),)


__all__ = [
    "DEFAULT_MUTABLE_TTL_S",
    "IMMUTABLE_TTL_S",
    "CacheClass",
    "CachePolicy",
    "EdgeCachePolicy",
    "classify_key",
    "invalidation_keys_for_shot",
]
