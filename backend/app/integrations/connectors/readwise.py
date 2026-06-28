"""Readwise highlights import — token auth, incremental, paginated.

Readwise's export API (``GET /api/v2/export/``) returns books, each with its
highlights, and supports two of the things the sync engine cares about:

* ``updatedAfter=<iso8601>`` — incremental fetch of only changed books, and
* ``pageCursor`` — opaque pagination.

Each Readwise *book* becomes one :class:`SourceItem`: the book title/author plus
its highlights as quote blocks (and any note as a note block). The book's
``highlights_url`` / its newest highlight timestamp drives the incremental
watermark. Auth is the user's Readwise access token, passed as
``Authorization: Token <token>`` — supplied via ``ctx.credential``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.integrations.connector import (
    Capability,
    ConnectorContext,
    ConnectorInfo,
    SourceConnector,
)
from app.integrations.errors import ConnectorError
from app.integrations.models import (
    BlockKind,
    FetchPage,
    NormalizedBlock,
    NormalizedDocument,
    SourceItem,
    SyncCursor,
)

_EXPORT_URL = "https://readwise.io/api/v2/export/"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _book_to_item(book: dict[str, Any]) -> SourceItem | None:
    """Map one Readwise export book record to a :class:`SourceItem`."""
    title = str(book.get("title") or book.get("readable_title") or "").strip()
    if not title:
        return None
    author = (str(book.get("author")).strip() or None) if book.get("author") else None
    highlights = book.get("highlights") or []
    blocks: list[NormalizedBlock] = [NormalizedBlock(kind=BlockKind.HEADING, text=title)]
    newest: datetime | None = None
    for h in highlights:
        if not isinstance(h, dict):
            continue
        text = str(h.get("text") or "").strip()
        if text:
            location = h.get("location")
            cite = f"Location {location}" if location else None
            blocks.append(NormalizedBlock(kind=BlockKind.QUOTE, text=text, cite=cite))
        note = str(h.get("note") or "").strip()
        if note:
            blocks.append(NormalizedBlock(kind=BlockKind.NOTE, text=note))
        ts = _parse_iso(h.get("highlighted_at") or h.get("updated"))
        if ts and (newest is None or ts > newest):
            newest = ts
    if len(blocks) == 1:  # heading only — no real content
        return None
    book_id = book.get("user_book_id") or book.get("id") or title
    doc = NormalizedDocument(
        title=title,
        author=author,
        blocks=tuple(blocks),
        metadata={
            "source": "readwise",
            "category": str(book.get("category") or ""),
            "highlights": str(len(highlights)),
        },
    )
    return SourceItem(
        source_id=f"readwise:{book_id}",
        document=doc,
        updated_at=newest,
        external_ref=str(book.get("source_url") or ""),
    )


class ReadwiseConnector(SourceConnector):
    """Import Readwise highlights, grouped one book per Readwise book."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="readwise",
            display_name="Readwise",
            capabilities=frozenset({Capability.TOKEN_AUTH, Capability.INCREMENTAL}),
            auth_hint="Paste your Readwise access token from readwise.io/access_token.",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        if not ctx.credential:
            raise ConnectorError("readwise import requires an access token (credential)")
        params: dict[str, Any] = {}
        if page_token:
            params["pageCursor"] = page_token
        elif cursor.high_watermark is not None:
            params["updatedAfter"] = cursor.high_watermark.isoformat()

        resp = await ctx.http.request(
            "GET",
            _EXPORT_URL,
            params=params,
            headers={"Authorization": f"Token {ctx.credential}", "Accept": "application/json"},
            timeout_s=45.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ConnectorError("readwise export returned an unexpected payload")

        items: list[SourceItem] = []
        for book in payload.get("results") or []:
            if isinstance(book, dict):
                item = _book_to_item(book)
                if item is not None:
                    items.append(item)
        next_cursor = payload.get("nextPageCursor")
        return FetchPage(
            items=tuple(items),
            next_cursor=str(next_cursor) if next_cursor else None,
        )


__all__ = ["ReadwiseConnector"]
