"""The read-model store seam + a deterministic in-memory implementation.

A read model is a denormalised view a projection maintains by folding events.
Where it *lives* is pluggable behind :class:`ReadModelStore`: tests and small
deployments use the in-memory store; production uses the Postgres-backed store
(:mod:`app.eventsourcing.projections.readmodel_pg`).

The store is a **namespaced key/value document store** with an optimistic
version stamp per row:

* ``namespace`` partitions one projection's rows from another's (typically the
  projection name, optionally suffixed with a blue/green slot — see
  :mod:`app.eventsourcing.projections.bluegreen`).
* ``key`` identifies a document within a namespace (e.g. a ``session_id``).
* ``value`` is a JSON-able dict (the materialised view row).
* ``version`` increments on every ``put`` of an existing key, giving cheap
  optimistic concurrency and a "did this change?" signal.

This shape is deliberately schemaless: every example projection (session
timeline, shot status board, canon audit) maps cleanly onto it, and a new
projection needs no migration of its own — it just claims a namespace.
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

    Rows live in ``{namespace: {key: ReadModelRow}}``. Values are deep-copied on
    the way in and out so a caller mutating a returned dict cannot corrupt stored
    state (the production store round-trips through JSON, which has the same
    isolation; the fake must match it).
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
        row = ReadModelRow(key=key, value=copy.deepcopy(value), version=version)
        bucket[key] = row
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

    # -- inspection ---------------------------------------------------------- #

    def namespaces(self) -> Sequence[str]:
        """Every namespace that currently holds at least one row.

        (Annotated ``Sequence[str]`` rather than ``list[str]`` because this class
        defines a :meth:`list` method that shadows the builtin under deferred
        annotation name resolution.)
        """
        return sorted(ns for ns, bucket in self._data.items() if bucket)


__all__ = [
    "InMemoryReadModelStore",
    "ReadModelRow",
    "ReadModelStore",
]
