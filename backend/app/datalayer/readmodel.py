"""The read-model store seam + a deterministic in-memory implementation.

A read model is a denormalised view a projection maintains by folding events.
Where it lives is pluggable behind :class:`ReadModelStore`: tests and embedded
deployments use :class:`InMemoryReadModelStore`; production uses a
Postgres-backed store over the ``datalayer_read_models`` table
(:mod:`app.datalayer.models`).

The store is a **namespaced key/value document store** with an optimistic
``version`` stamp per row:

* ``namespace`` partitions one projection's rows (its
  :attr:`~app.datalayer.projector.Projection.namespace`, optionally suffixed by a
  rebuild slot — see :mod:`app.datalayer.consistency`).
* ``key`` identifies a document within a namespace (e.g. a ``book_id``).
* ``value`` is a JSON-able ``dict`` (the materialised row).
* ``version`` increments on every ``put`` of an existing key.

The shape is deliberately schemaless so a new projection claims a namespace with
no migration of its own. Values are deep-copied in and out so a caller mutating a
returned dict cannot corrupt stored state (the Postgres store round-trips through
JSON, which gives the same isolation; the in-memory fake must match it).
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ReadModelRow:
    """One stored document: its key, JSON value, and optimistic version."""

    key: str
    value: dict[str, Any]
    version: int = 1


@runtime_checkable
class ReadModelStore(Protocol):
    """Namespaced key/value persistence for materialised read models."""

    async def get(self, namespace: str, key: str) -> ReadModelRow | None:
        """Return the row at ``(namespace, key)`` or ``None`` if absent."""
        ...

    async def put(self, namespace: str, key: str, value: dict[str, Any]) -> ReadModelRow:
        """Upsert ``value`` at ``(namespace, key)``; bump version; return the row."""
        ...

    async def delete(self, namespace: str, key: str) -> bool:
        """Delete the row; return whether it existed."""
        ...

    async def list(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        limit: int | None = None,
    ) -> list[ReadModelRow]:
        """Return rows in a namespace ordered by ``key`` (optionally prefix-filtered)."""
        ...

    async def clear(self, namespace: str) -> int:
        """Drop every row in a namespace; return how many were removed."""
        ...

    async def count(self, namespace: str) -> int:
        """Number of rows currently in a namespace."""
        ...


class InMemoryReadModelStore:
    """A deterministic, in-process :class:`ReadModelStore` (tests / embedded use).

    Rows live in ``{namespace: {key: ReadModelRow}}``. Deep-copy isolation on both
    arms mirrors the JSON round-trip of the production store.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, ReadModelRow]] = {}

    async def get(self, namespace: str, key: str) -> ReadModelRow | None:
        row = self._data.get(namespace, {}).get(key)
        if row is None:
            return None
        return ReadModelRow(key=row.key, value=copy.deepcopy(row.value), version=row.version)

    async def put(self, namespace: str, key: str, value: dict[str, Any]) -> ReadModelRow:
        bucket = self._data.setdefault(namespace, {})
        prior = bucket.get(key)
        version = (prior.version + 1) if prior is not None else 1
        bucket[key] = ReadModelRow(key=key, value=copy.deepcopy(value), version=version)
        return ReadModelRow(key=key, value=copy.deepcopy(value), version=version)

    async def delete(self, namespace: str, key: str) -> bool:
        bucket = self._data.get(namespace)
        if bucket is None or key not in bucket:
            return False
        del bucket[key]
        return True

    async def list(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        limit: int | None = None,
    ) -> list[ReadModelRow]:
        bucket = self._data.get(namespace, {})
        keys = sorted(bucket)
        if prefix is not None:
            keys = [k for k in keys if k.startswith(prefix)]
        if limit is not None:
            keys = keys[:limit]
        return [
            ReadModelRow(key=k, value=copy.deepcopy(bucket[k].value), version=bucket[k].version)
            for k in keys
        ]

    async def clear(self, namespace: str) -> int:
        bucket = self._data.get(namespace)
        if not bucket:
            return 0
        n = len(bucket)
        bucket.clear()
        return n

    async def count(self, namespace: str) -> int:
        return len(self._data.get(namespace, {}))

    # -- inspection (not part of the protocol) ------------------------------- #

    def namespaces(self) -> Sequence[str]:
        """Every namespace that currently holds at least one row (sorted)."""
        return sorted(ns for ns, bucket in self._data.items() if bucket)

    def snapshot(self, namespace: str) -> dict[str, dict[str, Any]]:
        """A ``{key: value}`` deep copy of a namespace (for consistency diffs)."""
        return {
            k: copy.deepcopy(row.value)
            for k, row in sorted(self._data.get(namespace, {}).items())
        }


__all__ = [
    "InMemoryReadModelStore",
    "ReadModelRow",
    "ReadModelStore",
]
