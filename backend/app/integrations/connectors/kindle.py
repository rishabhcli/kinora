"""Kindle "My Clippings.txt" import — a file-upload connector (no network).

A Kindle exports its highlights/notes as a plain-text ``My Clippings.txt`` whose
records are separated by a line of ten equals signs (``==========``). Each record
is::

    <Title> (<Author>)
    - Your Highlight on page 12 | Location 145-146 | Added on ...

    The highlighted text.
    ==========

This connector parses that file, groups records **by book title** (so all
highlights from one book become one Kinora book), and emits one
:class:`SourceItem` per book with its highlights as quote blocks. It declares
:class:`Capability.FILE_UPLOAD`: the bytes arrive in ``ctx.config['file']`` — no
network seam is used at all.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

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

_SEPARATOR = "=========="
#: "Title (Author)" — author is the last parenthesised group on the title line.
_TITLE_AUTHOR = re.compile(r"^(?P<title>.*?)\s*\((?P<author>[^()]*)\)\s*$")
#: The metadata line ("- Your Highlight on page 12 | Location ... | Added on ...").
_META_PREFIX = "- "


def _parse_clippings(text: str) -> OrderedDict[str, tuple[str | None, list[str]]]:
    """Group clipping records by book title → (author, [highlight texts])."""
    text = text.lstrip("﻿")  # strip a UTF-8 BOM if present
    books: OrderedDict[str, tuple[str | None, list[str]]] = OrderedDict()
    for record in text.split(_SEPARATOR):
        lines = [ln.rstrip("\r") for ln in record.split("\n")]
        lines = [ln for ln in lines if ln.strip() != ""]
        if not lines:
            continue
        title_line = lines[0].strip()
        m = _TITLE_AUTHOR.match(title_line)
        if m:
            title = m.group("title").strip()
            author = m.group("author").strip() or None
        else:
            title, author = title_line, None
        body = [ln.strip() for ln in lines[1:] if not ln.strip().startswith(_META_PREFIX)]
        highlight = " ".join(p for p in body if p).strip()
        if not title or not highlight:
            continue
        if title not in books:
            books[title] = (author, [])
        books[title][1].append(highlight)
    return books


class KindleClippingsConnector(SourceConnector):
    """Parse an uploaded Kindle ``My Clippings.txt`` into one book per title."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="kindle",
            display_name="Kindle Highlights",
            capabilities=frozenset({Capability.FILE_UPLOAD}),
            auth_hint="Upload the My Clippings.txt file from your Kindle's documents folder.",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        raw = ctx.cfg_bytes("file")
        if raw is None:
            raise ConnectorError(
                "kindle import requires an uploaded 'My Clippings.txt' (config['file'])"
            )
        try:
            text = bytes(raw).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - any decode failure => unusable
            raise ConnectorError(f"could not read clippings file: {exc}") from exc

        books = _parse_clippings(text)
        items: list[SourceItem] = []
        for title, (author, highlights) in books.items():
            blocks = [NormalizedBlock(kind=BlockKind.HEADING, text=title)]
            blocks.extend(
                NormalizedBlock(kind=BlockKind.QUOTE, text=h, cite=f"Highlight {i + 1}")
                for i, h in enumerate(highlights)
            )
            doc = NormalizedDocument(
                title=title,
                author=author,
                blocks=tuple(blocks),
                metadata={"source": "kindle", "highlights": str(len(highlights))},
            )
            source_id = "kindle:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
            items.append(SourceItem(source_id=source_id, document=doc))
        # One page; file upload is not paginated or incremental.
        return FetchPage(items=tuple(items), next_cursor=None)


__all__ = ["KindleClippingsConnector"]
