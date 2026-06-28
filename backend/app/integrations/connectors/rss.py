"""RSS / Atom feed import (+ OPML expansion), over the injected HTTP seam.

Two shapes feed in:

* a single feed URL (``config['feed_url']``), or
* an OPML outline (``config['opml']`` bytes) listing many feed URLs, each of
  which is fetched.

Each feed *entry* becomes a :class:`SourceItem`: the entry's title + author +
its content/summary HTML, extracted to readable blocks. Incremental sync uses
the newest entry ``published`` timestamp as the cursor high-watermark, plus the
HTTP ``ETag`` for a cheap "nothing changed" short-circuit. Parsing is stdlib
``xml.etree`` only — no ``feedparser`` dependency.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from app.integrations.connector import (
    Capability,
    ConnectorContext,
    ConnectorInfo,
    SourceConnector,
)
from app.integrations.errors import ConnectorError, PermanentError
from app.integrations.htmlutil import extract_article
from app.integrations.models import (
    BlockKind,
    FetchPage,
    NormalizedBlock,
    NormalizedDocument,
    SourceItem,
    SyncCursor,
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(value: str) -> datetime | None:
    """Parse an RSS (RFC822) or Atom (ISO8601) date to an aware UTC datetime."""
    value = value.strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_opml(data: bytes) -> list[str]:
    """Extract every feed URL (``xmlUrl`` attribute) from an OPML outline."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PermanentError(f"invalid OPML: {exc}") from exc
    urls: list[str] = []
    for outline in root.iter("outline"):
        url = outline.get("xmlUrl") or outline.get("xmlurl")
        if url:
            urls.append(url.strip())
    return urls


def _entry_blocks(title: str, html_or_text: str) -> tuple[NormalizedBlock, ...]:
    article = extract_article(html_or_text) if "<" in html_or_text else None
    blocks: list[NormalizedBlock] = [NormalizedBlock(kind=BlockKind.HEADING, text=title)]
    if article and article.blocks:
        for b in article.blocks:
            kind = {
                "heading": BlockKind.SUBHEADING,
                "subheading": BlockKind.SUBHEADING,
                "quote": BlockKind.QUOTE,
            }.get(b.role, BlockKind.PARAGRAPH)
            blocks.append(NormalizedBlock(kind=kind, text=b.text))
    else:
        clean = html_or_text.strip()
        if clean:
            blocks.append(NormalizedBlock(kind=BlockKind.PARAGRAPH, text=clean))
    return tuple(blocks)


def _parse_feed(xml: bytes, *, feed_url: str) -> tuple[list[SourceItem], str | None]:
    """Parse one RSS/Atom feed → (items, feed_title)."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise PermanentError(f"invalid feed XML at {feed_url}: {exc}") from exc

    items: list[SourceItem] = []
    channel = root.find("channel")
    if channel is not None:  # RSS 2.0
        feed_title = _text(channel.find("title")) or feed_url
        for it in channel.findall("item"):
            title = _text(it.find("title")) or "(untitled)"
            link = _text(it.find("link"))
            author = _text(it.find("author")) or _text(it.find(_DC_CREATOR)) or None
            content = _text(it.find(_CONTENT_ENCODED)) or _text(it.find("description"))
            guid = _text(it.find("guid")) or link or title
            published = _parse_date(_text(it.find("pubDate")))
            items.append(_make_item(guid, title, author, content, link, published, feed_title))
    else:  # Atom
        feed_title = _text(root.find(f"{_ATOM}title")) or feed_url
        for it in root.findall(f"{_ATOM}entry"):
            title = _text(it.find(f"{_ATOM}title")) or "(untitled)"
            link_el = it.find(f"{_ATOM}link")
            link = (link_el.get("href") if link_el is not None else "") or ""
            author = _text(it.find(f"{_ATOM}author/{_ATOM}name")) or None
            content = _text(it.find(f"{_ATOM}content")) or _text(it.find(f"{_ATOM}summary"))
            guid = _text(it.find(f"{_ATOM}id")) or link or title
            raw_date = _text(it.find(f"{_ATOM}updated")) or _text(it.find(f"{_ATOM}published"))
            published = _parse_date(raw_date)
            items.append(_make_item(guid, title, author, content, link, published, feed_title))
    return items, feed_title


def _make_item(
    guid: str,
    title: str,
    author: str | None,
    content: str,
    link: str,
    published: datetime | None,
    feed_title: str,
) -> SourceItem:
    doc = NormalizedDocument(
        title=title,
        author=author or feed_title,
        blocks=_entry_blocks(title, content),
        metadata={"source": "rss", "feed": feed_title, "url": link},
    )
    source_id = "rss:" + hashlib.sha1(guid.encode("utf-8")).hexdigest()[:20]
    return SourceItem(source_id=source_id, document=doc, updated_at=published, external_ref=link)


class RssConnector(SourceConnector):
    """Import RSS/Atom feed entries (single feed or an OPML of feeds)."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="rss",
            display_name="RSS / Atom",
            capabilities=frozenset({Capability.INCREMENTAL, Capability.FILE_UPLOAD}),
            auth_hint="Paste a feed URL, or upload an OPML file exported from your reader.",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        feed_urls = self._feed_urls(ctx)
        if not feed_urls:
            raise ConnectorError("rss import requires config['feed_url'] or config['opml']")

        all_items: list[SourceItem] = []
        first_etag: str | None = None
        for i, url in enumerate(feed_urls):
            headers: dict[str, str] = {}
            # Only one feed: support conditional GET via the stored etag.
            if len(feed_urls) == 1 and cursor.etag:
                headers["If-None-Match"] = cursor.etag
            resp = await ctx.http.request("GET", url, headers=headers, timeout_s=30.0)
            if resp.status == 304:
                return FetchPage(items=(), next_cursor=None, etag=cursor.etag)
            resp.raise_for_status()
            if i == 0:
                first_etag = resp.headers.get("etag")
            items, _ = _parse_feed(resp.content, feed_url=url)
            all_items.extend(items)

        kept = self._after_watermark(all_items, cursor.high_watermark)
        return FetchPage(items=tuple(kept), next_cursor=None, etag=first_etag)

    @staticmethod
    def _feed_urls(ctx: ConnectorContext) -> list[str]:
        opml = ctx.cfg_bytes("opml")
        if opml is not None:
            return parse_opml(bytes(opml))
        single = ctx.cfg_str("feed_url")
        return [single] if single else []

    @staticmethod
    def _after_watermark(
        items: list[SourceItem], watermark: datetime | None
    ) -> list[SourceItem]:
        if watermark is None:
            return items
        return [it for it in items if it.updated_at is None or it.updated_at > watermark]


__all__ = ["RssConnector", "parse_opml"]
