"""The pluggable :class:`VectorIndex` abstraction and an exact in-memory backend.

:class:`VectorIndex` is the seam the identity store, cache migration, and any
retrieval caller depend on. It supports:

* **upsert** of :class:`VectorRecord`\\ s (id + vector + metadata), idempotent on id;
* **k-NN** search by query vector with an optional :class:`MetadataFilter`;
* **namespaces** so vectors for one book / entity are isolated from another
  (a query in namespace A never returns a record from namespace B);
* **space guarding** — a record and a query must share a
  :class:`~app.embeddings.vectors.VectorSpace`, so a pgvector/HNSW backend can be
  swapped in without re-checking dimensions everywhere.

:class:`InMemoryVectorIndex` is a full, exact (brute-force) implementation with
no infrastructure — used by tests and any offline path. The protocol is shaped
so a ``PgVectorIndex`` (Postgres ``<=>`` operator) or an HNSW-backed index
satisfies the *same* interface; only the search internals differ.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from app.embeddings.vectors import EmbeddingVector, SpaceMismatch, VectorSpace

#: The default namespace when a caller does not scope by book/entity.
DEFAULT_NAMESPACE = "_default"


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """A stored vector: an id, its vector, and arbitrary metadata.

    ``metadata`` is a flat dict of JSON-ish scalars/lists used for filtering and
    for carrying back useful context (entity_key, version, modality, source key,
    pose tags, ...). ``namespace`` defaults to :data:`DEFAULT_NAMESPACE`.
    """

    id: str
    vector: EmbeddingVector
    namespace: str = DEFAULT_NAMESPACE
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def space(self) -> VectorSpace:
        return self.vector.space


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A k-NN hit: the matched record and its cosine score to the query."""

    record: VectorRecord
    score: float


class FilterOp(enum.StrEnum):
    """Supported metadata predicates."""

    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    GTE = "gte"
    LTE = "lte"
    EXISTS = "exists"
    CONTAINS = "contains"  # value is in a list-valued metadata field


@dataclass(frozen=True, slots=True)
class _Clause:
    key: str
    op: FilterOp
    value: Any = None


@dataclass(frozen=True, slots=True)
class MetadataFilter:
    """A conjunctive (AND) set of metadata predicates.

    Build fluently::

        MetadataFilter().eq("entity_key", "char_elsa").gte("version", 2)

    An empty filter matches everything. Filters are immutable; each builder
    method returns a new filter.
    """

    clauses: tuple[_Clause, ...] = ()

    def _add(self, clause: _Clause) -> MetadataFilter:
        return replace(self, clauses=(*self.clauses, clause))

    def eq(self, key: str, value: Any) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.EQ, value))

    def ne(self, key: str, value: Any) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.NE, value))

    def in_(self, key: str, values: Iterable[Any]) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.IN, tuple(values)))

    def not_in(self, key: str, values: Iterable[Any]) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.NOT_IN, tuple(values)))

    def gte(self, key: str, value: Any) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.GTE, value))

    def lte(self, key: str, value: Any) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.LTE, value))

    def exists(self, key: str) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.EXISTS))

    def contains(self, key: str, value: Any) -> MetadataFilter:
        return self._add(_Clause(key, FilterOp.CONTAINS, value))

    def matches(self, metadata: Mapping[str, Any]) -> bool:
        return all(_eval_clause(c, metadata) for c in self.clauses)

    @property
    def is_empty(self) -> bool:
        return not self.clauses


def _eval_clause(c: _Clause, md: Mapping[str, Any]) -> bool:
    present = c.key in md
    if c.op is FilterOp.EXISTS:
        return present
    if not present:
        # A predicate over an absent key fails (except EXISTS, handled above).
        return False
    actual = md[c.key]
    if c.op is FilterOp.EQ:
        return bool(actual == c.value)
    if c.op is FilterOp.NE:
        return bool(actual != c.value)
    if c.op is FilterOp.IN:
        return actual in c.value
    if c.op is FilterOp.NOT_IN:
        return actual not in c.value
    if c.op is FilterOp.GTE:
        return _cmp_ge(actual, c.value)
    if c.op is FilterOp.LTE:
        return _cmp_ge(c.value, actual)
    if c.op is FilterOp.CONTAINS:
        return isinstance(actual, (list, tuple, set)) and c.value in actual
    return False  # pragma: no cover - exhaustive above


def _cmp_ge(a: Any, b: Any) -> bool:
    try:
        return bool(a >= b)
    except TypeError:
        return False


@runtime_checkable
class VectorIndex(Protocol):
    """A namespaced, metadata-filterable vector index."""

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        """Insert/replace records (idempotent on id within a namespace)."""
        ...

    async def delete(self, ids: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE) -> int:
        """Remove records by id from a namespace; returns the count removed."""
        ...

    async def get(self, id: str, *, namespace: str = DEFAULT_NAMESPACE) -> VectorRecord | None:
        """Fetch one record by id, or ``None``."""
        ...

    async def search(
        self,
        query: EmbeddingVector,
        *,
        top_k: int,
        namespace: str = DEFAULT_NAMESPACE,
        filter: MetadataFilter | None = None,
    ) -> list[SearchResult]:
        """Return the ``top_k`` most-similar records in ``namespace``."""
        ...

    async def count(self, *, namespace: str | None = None) -> int:
        """Number of records in a namespace, or all namespaces if ``None``."""
        ...

    async def namespaces(self) -> list[str]:
        """All non-empty namespaces."""
        ...

    async def iter_records(
        self, *, namespace: str | None = None
    ) -> list[VectorRecord]:
        """Materialize all records (used by maintenance / migration)."""
        ...


class InMemoryVectorIndex:
    """Exact brute-force vector index. No infra; deterministic ordering.

    Storage is ``{namespace: {id: VectorRecord}}``. Search is a full scan with a
    space check and (optional) metadata filter, sorted by descending cosine with
    a stable tie-break on id so results are deterministic.
    """

    def __init__(self, *, expected_space: VectorSpace | None = None) -> None:
        #: If set, every upserted/queried vector must be in this space.
        self._expected_space = expected_space
        self._store: dict[str, dict[str, VectorRecord]] = {}

    # -- writes ------------------------------------------------------------- #
    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        for rec in records:
            self._guard_space(rec.vector.space)
            ns = self._store.setdefault(rec.namespace, {})
            ns[rec.id] = rec
        return len(records)

    async def delete(self, ids: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE) -> int:
        ns = self._store.get(namespace)
        if not ns:
            return 0
        removed = 0
        for rid in ids:
            if ns.pop(rid, None) is not None:
                removed += 1
        if not ns:
            self._store.pop(namespace, None)
        return removed

    async def drop_namespace(self, namespace: str) -> int:
        """Remove an entire namespace; returns the number of records dropped."""
        ns = self._store.pop(namespace, None)
        return len(ns) if ns else 0

    # -- reads -------------------------------------------------------------- #
    async def get(self, id: str, *, namespace: str = DEFAULT_NAMESPACE) -> VectorRecord | None:
        return self._store.get(namespace, {}).get(id)

    async def search(
        self,
        query: EmbeddingVector,
        *,
        top_k: int,
        namespace: str = DEFAULT_NAMESPACE,
        filter: MetadataFilter | None = None,
    ) -> list[SearchResult]:
        if top_k <= 0:
            return []
        self._guard_space(query.space)
        ns = self._store.get(namespace, {})
        hits: list[SearchResult] = []
        for rec in ns.values():
            # Namespace isolation is structural (we only scan ``ns``); the space
            # check guarantees we never score across embedders even if a caller
            # somehow mixed spaces into one namespace.
            if rec.vector.space != query.space:
                continue
            if filter is not None and not filter.matches(rec.metadata):
                continue
            hits.append(SearchResult(record=rec, score=query.cosine(rec.vector)))
        # Descending score, stable tie-break on id for determinism.
        hits.sort(key=lambda h: (-h.score, h.record.id))
        return hits[:top_k]

    async def count(self, *, namespace: str | None = None) -> int:
        if namespace is not None:
            return len(self._store.get(namespace, {}))
        return sum(len(ns) for ns in self._store.values())

    async def namespaces(self) -> list[str]:
        return sorted(ns for ns, recs in self._store.items() if recs)

    async def iter_records(self, *, namespace: str | None = None) -> list[VectorRecord]:
        if namespace is not None:
            return list(self._store.get(namespace, {}).values())
        out: list[VectorRecord] = []
        for recs in self._store.values():
            out.extend(recs.values())
        return out

    # -- internal ----------------------------------------------------------- #
    def _guard_space(self, space: VectorSpace) -> None:
        if self._expected_space is not None and space != self._expected_space:
            raise SpaceMismatch(
                f"index pinned to {self._expected_space.key} but got {space.key}"
            )


__all__ = [
    "DEFAULT_NAMESPACE",
    "FilterOp",
    "InMemoryVectorIndex",
    "MetadataFilter",
    "SearchResult",
    "VectorIndex",
    "VectorRecord",
]
