"""Opaque, tamper-evident cursor pagination (kinora.md §5 list surfaces).

Offset/limit pagination drifts when rows are inserted mid-scroll (the classic
"page 2 shows a row you already saw on page 1"). Cursor pagination is stable: a
cursor pins an *anchor* (here, a monotonic position — an event-log id, a shot
ordinal, a created-at timestamp) and the next page is "everything strictly after
the anchor". This module is the transport contract for that:

* :func:`encode_cursor` / :func:`decode_cursor` — a base64url **signed** token
  carrying an opaque dict. The signature (HMAC over the JWT secret) makes the
  cursor tamper-evident: a client can't forge one to read another scope's data,
  and a corrupted/old-format cursor is rejected cleanly (422) rather than
  mis-parsed. The token is URL-safe and stripped of ``=`` padding so it survives
  a query string untouched.
* :class:`Page` — the response envelope: ``items`` + ``next_cursor`` (``null`` at
  the end) + ``has_more``. Bare-list endpoints stay bare; the *new* paginated
  endpoints return this envelope.
* :func:`paginate_after` — a tiny helper that slices an already-ordered sequence
  by an integer anchor and mints the next cursor, so a route is one line.

The cursor is intentionally not a DB keyset SQL fragment — it is a transport
value. A route decodes it to a typed anchor and does its own ordered query/slice,
which keeps the contract storage-agnostic (Redis log, Postgres rows, in-memory).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

#: Hard ceiling on a page size, regardless of the client's request.
MAX_PAGE_SIZE = 200
#: Default page size when the client doesn't specify one.
DEFAULT_PAGE_SIZE = 50


class CursorError(ValueError):
    """Raised when a cursor is malformed, mis-signed, or stale-format."""


def _sign(body: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()[:16]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(token: str) -> bytes:
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + pad)
    except (binascii.Error, ValueError) as exc:
        raise CursorError("malformed cursor") from exc


def encode_cursor(payload: dict[str, Any], *, secret: str) -> str:
    """Encode ``payload`` into a signed, URL-safe opaque cursor token.

    Layout: ``b64url(sig[16] || json_body)``. The 16-byte HMAC prefix is verified
    on decode, so a tampered or foreign-secret cursor is rejected.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64encode(_sign(body, secret) + body)


def decode_cursor(token: str, *, secret: str) -> dict[str, Any]:
    """Decode + verify a cursor token (raises :class:`CursorError` on any problem)."""
    raw = _b64decode(token)
    if len(raw) <= 16:
        raise CursorError("truncated cursor")
    sig, body = raw[:16], raw[16:]
    if not hmac.compare_digest(sig, _sign(body, secret)):
        raise CursorError("cursor signature mismatch")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CursorError("undecodable cursor body") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor body is not an object")
    return payload


def clamp_limit(limit: int | None) -> int:
    """Clamp a requested page size into ``[1, MAX_PAGE_SIZE]`` (default-filled)."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(limit), MAX_PAGE_SIZE))


class Page(BaseModel, Generic[T]):
    """A page of results: items + the cursor for the next page (``null`` at end)."""

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True, slots=True)
class IntSlice(Generic[T]):
    """The result of slicing an ordered sequence by an integer anchor."""

    items: list[T]
    next_anchor: int | None
    has_more: bool


def paginate_after(
    ordered: list[T],
    *,
    after: int | None,
    limit: int,
    key: Any,
) -> IntSlice[T]:
    """Slice an *ascending-ordered* sequence to the page strictly after ``after``.

    ``key`` extracts the comparable integer anchor from an item. Returns the page
    plus the anchor for the next page (``None`` when exhausted). This is the pure,
    storage-free core the routes reuse for log-id / ordinal pagination.
    """
    start = 0
    if after is not None:
        # First item whose key is strictly greater than the anchor.
        while start < len(ordered) and int(key(ordered[start])) <= after:
            start += 1
    window = ordered[start : start + limit]
    has_more = (start + limit) < len(ordered)
    next_anchor = int(key(window[-1])) if window and has_more else None
    return IntSlice(items=window, next_anchor=next_anchor, has_more=has_more)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "CursorError",
    "IntSlice",
    "Page",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "paginate_after",
]
