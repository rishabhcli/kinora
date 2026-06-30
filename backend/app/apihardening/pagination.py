"""Cursor-based pagination — opaque signed cursors + a generic ``Page[T]``.

Keyset / cursor pagination beats ``OFFSET`` at scale: a cursor encodes "the row
after this sort key" so page N+1 is an indexed range scan, immune to drift when
rows are inserted/deleted mid-scroll. This module gives:

* :class:`Cursor` — the decoded payload (sort key + optional tie-breaker id +
  direction), versioned for forward-compat.
* :class:`CursorCodec` — encodes a cursor to an **opaque, URL-safe, tamper-evident
  token** (HMAC-signed with the app secret) and decodes+verifies it back. A
  client treats the token as opaque; a tampered token is rejected, so a cursor
  can't be used to smuggle an attacker-chosen sort key past a query.
* :class:`Page` — a generic ``Page[T]`` envelope (``items`` + ``page`` meta with
  ``next_cursor`` / ``has_more``) that *new* endpoints can adopt without touching
  the existing bare-``list`` responses the renderer parses today.
* :func:`paginate` — slice an in-memory sequence into a ``Page`` (handy for tests
  and small collections); the codec is what a DB-backed keyset query uses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.apihardening.problem import ProblemException

T = TypeVar("T")

#: Bump when the cursor payload schema changes incompatibly.
CURSOR_VERSION = 1
_SIG_BYTES = 16  # truncated HMAC-SHA256 — plenty to defeat tampering for a cursor


class Cursor(BaseModel):
    """The decoded contents of a pagination cursor.

    ``key`` is the value of the sort column on the boundary row; ``id`` is an
    optional stable tie-breaker (so equal sort keys paginate deterministically);
    ``direction`` is ``"next"`` (forward) or ``"prev"`` (backward).
    """

    model_config = ConfigDict(extra="forbid")

    v: int = CURSOR_VERSION
    key: Any = None
    id: str | None = None
    direction: str = "next"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(token: str) -> bytes:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding)


class CursorCodec:
    """Encode/decode opaque, HMAC-signed pagination cursors.

    The token layout is ``b64url(payload) + "." + b64url(sig)`` where ``sig`` is a
    truncated HMAC-SHA256 over the payload bytes, keyed by ``secret``. Decoding
    verifies the signature in constant time and rejects tampering with a typed
    422 problem.
    """

    def __init__(self, secret: str, *, namespace: str = "kinora:cursor") -> None:
        # Bind the signing key to a namespace so a cursor minted for one endpoint
        # cannot be replayed against another that shares the app secret.
        self._key = hashlib.sha256(f"{namespace}:{secret}".encode()).digest()

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()[:_SIG_BYTES]

    def encode(self, cursor: Cursor) -> str:
        """Encode ``cursor`` to an opaque, signed, URL-safe token."""
        payload = json.dumps(
            cursor.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        sig = self._sign(payload)
        return f"{_b64encode(payload)}.{_b64encode(sig)}"

    def decode(self, token: str) -> Cursor:
        """Decode + verify a token back into a :class:`Cursor`.

        Raises :class:`ProblemException` (422 ``invalid_cursor``) on any malformed
        or tampered token — never on a stack trace the client could probe.
        """
        if not token or "." not in token:
            raise self._invalid()
        payload_b64, sig_b64 = token.split(".", 1)
        try:
            payload = _b64decode(payload_b64)
            sig = _b64decode(sig_b64)
        except (binascii.Error, ValueError) as exc:
            raise self._invalid() from exc
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise self._invalid()
        try:
            data = json.loads(payload.decode("utf-8"))
            cursor = Cursor.model_validate(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._invalid() from exc
        if cursor.v != CURSOR_VERSION:
            raise self._invalid("cursor version is no longer supported")
        return cursor

    @staticmethod
    def _invalid(detail: str = "the pagination cursor is malformed or has been tampered with") -> (
        ProblemException
    ):
        return ProblemException(
            "invalid_cursor", "Invalid pagination cursor", status=422, detail=detail
        )


class PageMeta(BaseModel):
    """Pagination metadata for a :class:`Page` (the cursors a client follows)."""

    limit: int
    count: int
    has_more: bool = False
    next_cursor: str | None = None
    prev_cursor: str | None = None


class Page(BaseModel, Generic[T]):
    """A generic page envelope for cursor-paginated collections.

    New endpoints return ``Page[ShotResponse]`` etc.; the envelope is *additive*
    — existing bare-``list`` endpoints are untouched, so no current client breaks.
    """

    items: list[T] = Field(default_factory=list)
    page: PageMeta


def encode_cursor(codec: CursorCodec, *, key: Any, id: str | None = None) -> str:
    """Convenience: encode a forward cursor for a boundary row."""
    return codec.encode(Cursor(key=key, id=id, direction="next"))


def decode_cursor(codec: CursorCodec, token: str | None) -> Cursor | None:
    """Convenience: decode ``token`` (``None``/empty -> ``None``, no first page cursor)."""
    if not token:
        return None
    return codec.decode(token)


def paginate(
    items: list[T],
    *,
    limit: int,
    codec: CursorCodec | None = None,
    key_of: Any = None,
    id_of: Any = None,
) -> Page[T]:
    """Slice an already-sorted, already-windowed ``items`` list into a ``Page``.

    Pass ``limit + 1`` rows from your query: this trims the extra row, sets
    ``has_more`` accordingly, and (when a ``codec`` + ``key_of`` are given) mints
    the ``next_cursor`` off the last kept row. Pure + deterministic.
    """
    has_more = len(items) > limit
    kept = items[:limit]
    next_cursor: str | None = None
    if has_more and codec is not None and key_of is not None and kept:
        boundary = kept[-1]
        key = key_of(boundary)
        row_id = id_of(boundary) if id_of is not None else None
        next_cursor = encode_cursor(codec, key=key, id=row_id)
    return Page[T](
        items=kept,
        page=PageMeta(
            limit=limit,
            count=len(kept),
            has_more=has_more,
            next_cursor=next_cursor,
        ),
    )


__all__ = [
    "CURSOR_VERSION",
    "Cursor",
    "CursorCodec",
    "Page",
    "PageMeta",
    "decode_cursor",
    "encode_cursor",
    "paginate",
]
