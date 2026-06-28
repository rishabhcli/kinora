"""Versioned index aliases — zero-downtime bulk reindex (the alias swap).

A search index is referenced by a stable **alias** (``kinora_current``); the
alias resolves to a concrete **index version** (``v1``, ``v2``, …). A bulk
reindex:

1. allocates a *fresh* version (a new ``index_version`` value),
2. builds the entire corpus into it (live reads are untouched — they still
   resolve the alias to the old version),
3. atomically repoints the alias at the new version (one row update),
4. optionally drops the now-orphaned old version.

This mirrors how Elasticsearch / OpenSearch use index aliases for reindexing.
The :class:`AliasRegistry` persists the alias→version map in the
``search_index_aliases`` table so every API instance resolves the same live
version. An in-memory variant backs the offline/in-memory index.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models.search import SearchIndexAlias

#: The default alias every reader resolves to find the live index version.
DEFAULT_ALIAS = "kinora_current"

SessionFactory = Any


def new_version(prefix: str = "v") -> str:
    """Allocate a fresh, monotonic-ish index version id (``v{ms-timestamp}``)."""
    return f"{prefix}{int(time.time() * 1000)}"


@runtime_checkable
class AliasRegistry(Protocol):
    """Resolve and atomically repoint an index alias to a concrete version."""

    async def resolve(self, alias: str = DEFAULT_ALIAS) -> str | None:
        """Return the live index version for ``alias`` (``None`` if unset)."""
        ...

    async def set_alias(self, alias: str, index_version: str) -> None:
        """Atomically point ``alias`` at ``index_version`` (the reindex swap)."""
        ...


class PostgresAliasRegistry:
    """Persist alias→version in ``search_index_aliases`` (shared across instances)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve(self, alias: str = DEFAULT_ALIAS) -> str | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SearchIndexAlias.index_version).where(SearchIndexAlias.alias == alias)
            )
            return row

    async def set_alias(self, alias: str, index_version: str) -> None:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(SearchIndexAlias)
                .values(alias=alias, index_version=index_version)
                .on_conflict_do_update(
                    index_elements=[SearchIndexAlias.alias],
                    set_={"index_version": index_version},
                )
            )
            await session.execute(stmt)


class InMemoryAliasRegistry:
    """A process-local alias map (for the in-memory index / tests)."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._map: dict[str, str] = dict(initial or {})

    async def resolve(self, alias: str = DEFAULT_ALIAS) -> str | None:
        return self._map.get(alias)

    async def set_alias(self, alias: str, index_version: str) -> None:
        self._map[alias] = index_version


__all__ = [
    "DEFAULT_ALIAS",
    "AliasRegistry",
    "InMemoryAliasRegistry",
    "PostgresAliasRegistry",
    "new_version",
]
