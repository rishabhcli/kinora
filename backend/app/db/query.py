"""Query-building helpers: pagination, filtering, ordering, IN-chunking.

Reusable, side-effect-free SQL shaping the concrete repositories and the generic
repository compose. None of these execute anything — they take and return
``Select`` statements (or chunk iterables), so they are pure and trivially
unit-testable by compiling the SQL.

* :func:`apply_filters` — equality / list-membership / comparison filters from a
  declarative ``{column: value}`` (or ``{column__op: value}``) mapping.
* :func:`apply_ordering` — multi-column ordering from ``"col"`` / ``"-col"`` keys
  with a per-statement allow-list so a caller can't order by an arbitrary column.
* :func:`paginate` / :class:`Page` — classic ``LIMIT``/``OFFSET`` paging with a
  metadata-carrying result. Offset paging is simple but O(offset); for deep
  scans use keyset paging.
* :func:`keyset_paginate` / :class:`Cursor` — cursor/seek pagination over a
  monotonic key (the §4.2 source-span index and the library shelf both have a
  natural sort key), which stays O(page size) at any depth.
* :func:`chunked` / :func:`in_chunks` — split a large ``IN (...)`` list into
  bounded batches so a query never exceeds Postgres' parameter limit.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, asc, desc, func, select
from sqlalchemy.orm import InstrumentedAttribute

RowT = TypeVar("RowT")

# Comparison operators addressable via the ``column__op`` filter syntax.
_OPERATORS: dict[str, str] = {
    "eq": "__eq__",
    "ne": "__ne__",
    "lt": "__lt__",
    "le": "__le__",
    "gt": "__gt__",
    "ge": "__ge__",
    "in": "in_",
    "not_in": "not_in",
    "like": "like",
    "ilike": "ilike",
    "is": "is_",
    "is_not": "is_not",
}

# Postgres caps bind parameters per statement at 65535; keep IN-lists well under
# that so a single batch can carry other params too.
DEFAULT_IN_CHUNK = 1000


def _resolve_column(model: type[Any], name: str) -> InstrumentedAttribute[Any]:
    """Resolve ``name`` to a mapped column on ``model`` (raises on unknown column)."""
    column = getattr(model, name, None)
    if not isinstance(column, InstrumentedAttribute):
        raise ValueError(f"{model.__name__} has no mapped column {name!r}")
    return column


def apply_filters(stmt: Select[Any], model: type[Any], filters: Mapping[str, Any]) -> Select[Any]:
    """Add ``WHERE`` clauses from a declarative filter mapping.

    Keys are ``"column"`` (defaults to equality, or ``IN`` when the value is a
    list/tuple/set) or ``"column__op"`` for an explicit operator (``gt``, ``le``,
    ``ilike``, ``in``, ``is_not``, …). Unknown columns/operators raise so a typo
    fails loudly rather than silently widening the result set.
    """
    for key, value in filters.items():
        column_name, _, op = key.partition("__")
        column = _resolve_column(model, column_name)
        if not op:
            if isinstance(value, (list, tuple, set, frozenset)):
                stmt = stmt.where(column.in_(list(value)))
            else:
                stmt = stmt.where(column == value)
            continue
        method = _OPERATORS.get(op)
        if method is None:
            raise ValueError(f"unsupported filter operator {op!r} in {key!r}")
        stmt = stmt.where(getattr(column, method)(value))
    return stmt


def apply_ordering(
    stmt: Select[Any],
    model: type[Any],
    order_by: Sequence[str],
    *,
    allowed: Iterable[str] | None = None,
) -> Select[Any]:
    """Add ``ORDER BY`` from ``"col"`` / ``"-col"`` keys (``-`` = descending).

    When ``allowed`` is given, ordering by any other column raises — the guard a
    public-facing ``?sort=`` parameter needs so it can't order by an unindexed or
    sensitive column.
    """
    allow_set = set(allowed) if allowed is not None else None
    for key in order_by:
        descending = key.startswith("-")
        name = key[1:] if descending else key
        if allow_set is not None and name not in allow_set:
            raise ValueError(f"ordering by {name!r} is not allowed")
        column = _resolve_column(model, name)
        stmt = stmt.order_by(desc(column) if descending else asc(column))
    return stmt


@dataclass(slots=True)
class Page(Generic[RowT]):
    """One page of results plus the paging metadata a UI/API needs."""

    items: list[RowT]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        """True when rows remain beyond this page."""
        return self.offset + len(self.items) < self.total

    @property
    def page_number(self) -> int:
        """1-indexed page number derived from offset/limit."""
        if self.limit <= 0:
            return 1
        return (self.offset // self.limit) + 1

    @property
    def num_pages(self) -> int:
        """Total page count for the current limit."""
        if self.limit <= 0:
            return 1
        return max(1, (self.total + self.limit - 1) // self.limit)


def paginate(stmt: Select[Any], *, limit: int, offset: int = 0) -> Select[Any]:
    """Apply ``LIMIT``/``OFFSET`` to ``stmt`` (clamps negatives to safe values)."""
    safe_limit = max(0, limit)
    safe_offset = max(0, offset)
    return stmt.limit(safe_limit).offset(safe_offset)


def count_statement(stmt: Select[Any]) -> Select[Any]:
    """Build a ``SELECT count(*)`` over the same filtered set as ``stmt``.

    Strips ordering/limit/offset (they don't affect a count and ``ORDER BY`` in a
    subquery is wasted work) and wraps the remaining query as a subquery so the
    count reflects exactly the rows ``stmt`` would return.
    """
    inner = stmt.order_by(None).limit(None).offset(None).subquery()
    return select(func.count()).select_from(inner)


@dataclass(slots=True)
class Cursor:
    """An opaque keyset cursor: the sort key of the last row of the prior page."""

    last_value: Any
    descending: bool = False


def keyset_paginate(
    stmt: Select[Any],
    model: type[Any],
    *,
    key: str,
    limit: int,
    after: Cursor | None = None,
    descending: bool = False,
) -> Select[Any]:
    """Seek-paginate ``stmt`` over a monotonic ``key`` column (O(page size)).

    The first page passes ``after=None``; each subsequent page passes a
    :class:`Cursor` carrying the previous page's last key value. Far cheaper than
    deep ``OFFSET`` because Postgres seeks the index to ``key > last`` rather than
    counting past every skipped row. ``key`` must be unique-enough to be a stable
    sort (a primary key or a monotonic index column).
    """
    column = _resolve_column(model, key)
    stmt = stmt.order_by(desc(column) if descending else asc(column))
    if after is not None:
        stmt = stmt.where(column < after.last_value if descending else column > after.last_value)
    return stmt.limit(max(0, limit))


def chunked(items: Sequence[RowT], size: int = DEFAULT_IN_CHUNK) -> Iterator[list[RowT]]:
    """Yield ``items`` in lists of at most ``size`` (for batched IN / bulk ops)."""
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def in_chunks(
    model: type[Any], column_name: str, values: Iterable[Any], *, size: int = DEFAULT_IN_CHUNK
) -> Iterator[Any]:
    """Yield ``column IN (chunk)`` clauses splitting ``values`` into bounded batches.

    The caller runs one query per yielded clause (or ``OR``s them) so a huge id
    list never overflows the bind-parameter ceiling. Deduplicates while
    preserving first-seen order.
    """
    column = _resolve_column(model, column_name)
    seen: set[Any] = set()
    ordered: list[Any] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    for chunk in chunked(ordered, size):
        yield column.in_(chunk)


__all__ = [
    "DEFAULT_IN_CHUNK",
    "Cursor",
    "Page",
    "apply_filters",
    "apply_ordering",
    "chunked",
    "count_statement",
    "in_chunks",
    "keyset_paginate",
    "paginate",
]
