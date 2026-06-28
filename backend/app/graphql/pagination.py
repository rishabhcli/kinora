"""Relay-style cursor connections with opaque, stable cursors.

A *connection* is the spec-blessed shape for paginated lists:

```
type XConnection { edges: [XEdge!]!  pageInfo: PageInfo!  totalCount: Int }
type XEdge { node: X!  cursor: Cursor! }
type PageInfo { hasNextPage  hasPreviousPage  startCursor  endCursor }
```

Cursors are **opaque**: a base64 of ``cursor:<offset>``. The gateway paginates
over already-loaded, deterministically-ordered lists (the domain repositories
return small, ordered collections), so an offset cursor is correct and simple.
``connection_from_list`` slices a list by ``first``/``after`` (and ``last``/
``before``) and builds the edges + ``pageInfo`` + ``totalCount``.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.graphql.errors import bad_input

T = TypeVar("T")

_PREFIX = "cursor:"
#: Hard cap on a page size so a single field can never request an unbounded slice.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def encode_cursor(offset: int) -> str:
    """Encode a 0-based list offset into an opaque base64 cursor."""
    return base64.urlsafe_b64encode(f"{_PREFIX}{offset}".encode()).decode("ascii")


def decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor back to its offset (raises ``BAD_USER_INPUT``)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise bad_input(f"Invalid cursor {cursor!r}.") from exc
    if not raw.startswith(_PREFIX):
        raise bad_input(f"Invalid cursor {cursor!r}.")
    try:
        return int(raw[len(_PREFIX) :])
    except ValueError as exc:
        raise bad_input(f"Invalid cursor {cursor!r}.") from exc


@dataclass(slots=True)
class PageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_cursor: str | None
    end_cursor: str | None


@dataclass(slots=True)
class Edge(Generic[T]):
    node: T
    cursor: str


@dataclass(slots=True)
class Connection(Generic[T]):
    edges: list[Edge[T]]
    page_info: PageInfo
    total_count: int


def _clamp_first(first: int | None) -> int:
    if first is None:
        return DEFAULT_PAGE_SIZE
    if first < 0:
        raise bad_input("`first` must be non-negative.")
    return min(first, MAX_PAGE_SIZE)


def effective_page_size(first: int | None, last: int | None = None) -> int:
    """The page size a complexity estimator should bill for this connection."""
    if first is not None:
        return _clamp_first(first)
    if last is not None and last >= 0:
        return min(last, MAX_PAGE_SIZE)
    return DEFAULT_PAGE_SIZE


def connection_from_list(
    items: Sequence[T],
    *,
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
) -> Connection[T]:
    """Build a Relay connection from an in-memory, ordered ``items`` list.

    Supports forward (``first``/``after``) and backward (``last``/``before``)
    paging over the same offset cursor space. ``first`` defaults to
    ``DEFAULT_PAGE_SIZE`` and is hard-capped at ``MAX_PAGE_SIZE``.
    """
    total = len(items)
    start = 0
    end = total
    if after is not None:
        start = max(start, decode_cursor(after) + 1)
    if before is not None:
        end = min(end, decode_cursor(before))
    start = min(start, total)
    end = max(start, min(end, total))

    if first is not None:
        end = min(end, start + _clamp_first(first))
    if last is not None:
        if last < 0:
            raise bad_input("`last` must be non-negative.")
        start = max(start, end - min(last, MAX_PAGE_SIZE))

    edges = [Edge(node=items[i], cursor=encode_cursor(i)) for i in range(start, end)]
    has_next = end < total
    has_prev = start > 0
    page_info = PageInfo(
        has_next_page=has_next,
        has_previous_page=has_prev,
        start_cursor=edges[0].cursor if edges else None,
        end_cursor=edges[-1].cursor if edges else None,
    )
    return Connection(edges=edges, page_info=page_info, total_count=total)


def edge_node(edge: Edge[Any]) -> Any:
    return edge.node


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Connection",
    "Edge",
    "PageInfo",
    "connection_from_list",
    "decode_cursor",
    "edge_node",
    "effective_page_size",
    "encode_cursor",
]
