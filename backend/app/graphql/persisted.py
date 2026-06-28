"""Persisted queries + Automatic Persisted Queries (APQ).

Persisted queries let a client send a short ``sha256`` hash instead of the full
query text — smaller requests, and an *allow-list* surface (only registered
operations can run when the registry is in strict mode). Two flows:

* **explicit** — the client sends ``{"id": "<sha256>"}``; the gateway looks up the
  registered document or returns ``PERSISTED_QUERY_NOT_FOUND``;
* **APQ** — the client first sends only ``extensions.persistedQuery.sha256Hash``;
  on a miss the gateway answers ``PERSISTED_QUERY_NOT_FOUND`` so the client
  retries with the full ``query`` *plus* the hash, which the gateway verifies and
  registers for next time.

The registry is Redis-backed (no DB migration) with an in-process LRU in front so
hot queries skip Redis. ``compute_hash`` is the canonical sha256 of the query
string (the client computes the same).
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING

from app.graphql.errors import ErrorCode, GraphQLError

if TYPE_CHECKING:
    from app.redis.client import RedisClient

_REDIS_PREFIX = "kinora:gql:pq:"
_LRU_MAX = 256


def compute_hash(query: str) -> str:
    """The canonical sha256 (hex) of a query string — the persisted-query id."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


class PersistedQueryError(GraphQLError):
    """A persisted-query miss the client should react to (APQ handshake)."""

    def __init__(self, message: str = "PersistedQueryNotFound") -> None:
        super().__init__(message, code=ErrorCode.PERSISTED_QUERY_NOT_FOUND)


class PersistedQueryStore:
    """A Redis-backed persisted-query registry with an in-process LRU cache."""

    def __init__(self, redis: RedisClient, *, ttl_s: int = 30 * 24 * 3600) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._lru: OrderedDict[str, str] = OrderedDict()

    async def get(self, sha256: str) -> str | None:
        cached = self._lru.get(sha256)
        if cached is not None:
            self._lru.move_to_end(sha256)
            return cached
        raw = await self._redis.get_json(_REDIS_PREFIX + sha256)
        if isinstance(raw, dict) and isinstance(raw.get("query"), str):
            query = raw["query"]
            self._remember(sha256, query)
            return query
        return None

    async def register(self, query: str, *, expected_hash: str | None = None) -> str:
        """Register a query under its sha256 (verifying ``expected_hash`` when given)."""
        sha256 = compute_hash(query)
        if expected_hash is not None and expected_hash != sha256:
            raise GraphQLError(
                "Provided sha256Hash does not match the query.",
                code=ErrorCode.BAD_USER_INPUT,
            )
        await self._redis.set_json(_REDIS_PREFIX + sha256, {"query": query}, ttl_s=self._ttl_s)
        self._remember(sha256, query)
        return sha256

    def _remember(self, sha256: str, query: str) -> None:
        self._lru[sha256] = query
        self._lru.move_to_end(sha256)
        while len(self._lru) > _LRU_MAX:
            self._lru.popitem(last=False)


def extract_apq_hash(extensions: dict[str, object] | None) -> str | None:
    """Pull ``extensions.persistedQuery.sha256Hash`` from a request body, if present."""
    if not isinstance(extensions, dict):
        return None
    pq = extensions.get("persistedQuery")
    if isinstance(pq, dict):
        sha = pq.get("sha256Hash")
        if isinstance(sha, str):
            return sha
    return None


async def resolve_query_text(
    store: PersistedQueryStore,
    *,
    query: str | None,
    persisted_id: str | None,
    apq_hash: str | None,
) -> str:
    """Resolve the query text to execute, applying the explicit + APQ flows.

    Precedence: explicit ``id`` → APQ hash → inline ``query``. Raises
    :class:`PersistedQueryError` on a hash miss (the client re-sends with text),
    or a ``BAD_USER_INPUT`` GraphQLError when nothing usable is provided.
    """
    if persisted_id:
        found = await store.get(persisted_id)
        if found is None:
            raise PersistedQueryError()
        return found
    if apq_hash:
        found = await store.get(apq_hash)
        if found is not None:
            return found
        if query is None:
            # APQ probe: ask the client to resend the full query with the hash.
            raise PersistedQueryError()
        await store.register(query, expected_hash=apq_hash)
        return query
    if query is None:
        raise GraphQLError(
            "Must provide `query` or a persisted-query id.",
            code=ErrorCode.BAD_USER_INPUT,
        )
    return query


__all__ = [
    "PersistedQueryError",
    "PersistedQueryStore",
    "compute_hash",
    "extract_apq_hash",
    "resolve_query_text",
]
