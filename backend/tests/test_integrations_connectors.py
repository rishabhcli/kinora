"""Unit tests for every source connector (offline, via FakeHttpClient/file bytes).

Each connector is exercised through its public ``fetch_page`` / ``iter_items``
with canned responses, asserting it normalizes into the expected
:class:`SourceItem`s. No real network is ever touched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.connector import Capability, ConnectorContext
from app.integrations.connectors.kindle import KindleClippingsConnector
from app.integrations.connectors.notion import NotionConnector, blocks_to_normalized
from app.integrations.connectors.pocket import PocketConnector
from app.integrations.connectors.readwise import ReadwiseConnector
from app.integrations.connectors.rss import RssConnector, parse_opml
from app.integrations.connectors.web import WebArticleConnector, article_to_item
from app.integrations.errors import ConnectorError, PermanentError
from app.integrations.http import FakeHttpClient, HttpResponse
from app.integrations.models import BlockKind, SyncCursor
from app.integrations.registry import default_registry


def _ctx(http: FakeHttpClient | None = None, **config: object) -> ConnectorContext:
    return ConnectorContext(http=http or FakeHttpClient(), config=dict(config))


def _headers(http: FakeHttpClient, i: int = 0) -> dict[str, str]:
    h = http.requests[i].headers
    assert h is not None
    return h


def _json(http: FakeHttpClient, i: int = 0) -> dict[str, Any]:
    j = http.requests[i].json
    assert isinstance(j, dict)
    return j


def _params(http: FakeHttpClient, i: int = 0) -> dict[str, Any]:
    p = http.requests[i].params
    assert p is not None
    return p


async def _all_items(connector, ctx, cursor=None):  # type: ignore[no-untyped-def]
    return [item async for item in connector.iter_items(ctx, cursor or SyncCursor())]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_has_all_connectors() -> None:
    reg = default_registry()
    assert set(reg.names()) == {"kindle", "notion", "pocket", "readwise", "rss", "web"}
    assert reg.info("readwise").supports(Capability.INCREMENTAL)
    assert reg.info("kindle").supports(Capability.FILE_UPLOAD)


# --------------------------------------------------------------------------- #
# Kindle
# --------------------------------------------------------------------------- #
_CLIPPINGS = """﻿The Republic (Plato)
- Your Highlight on page 12 | Location 145-146 | Added on Monday

Justice is the excellence of the soul.
==========
The Republic (Plato)
- Your Highlight Location 200

The unexamined life is not worth living.
==========
Meditations (Marcus Aurelius)
- Your Note Location 5

You have power over your mind.
==========
"""


@pytest.mark.asyncio
async def test_kindle_groups_by_title() -> None:
    connector = KindleClippingsConnector()
    items = await _all_items(connector, _ctx(file=_CLIPPINGS.encode("utf-8")))
    by_title = {i.document.title: i for i in items}
    assert set(by_title) == {"The Republic", "Meditations"}
    republic = by_title["The Republic"]
    assert republic.document.author == "Plato"
    quotes = [b.text for b in republic.document.blocks if b.kind is BlockKind.QUOTE]
    assert len(quotes) == 2
    assert "Justice is the excellence" in quotes[0]


@pytest.mark.asyncio
async def test_kindle_requires_file() -> None:
    with pytest.raises(ConnectorError):
        await _all_items(KindleClippingsConnector(), _ctx())


# --------------------------------------------------------------------------- #
# Readwise
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_readwise_paginates_and_normalizes() -> None:
    page1 = {
        "results": [
            {
                "user_book_id": 1,
                "title": "Deep Work",
                "author": "Cal Newport",
                "category": "books",
                "highlights": [
                    {"text": "Focus is the new IQ.", "location": 42,
                     "highlighted_at": "2026-01-02T00:00:00Z", "note": "remember this"},
                ],
            }
        ],
        "nextPageCursor": "cur2",
    }
    page2: dict[str, Any] = {"results": [], "nextPageCursor": None}
    http = FakeHttpClient().add(
        "GET",
        "/export",
        [
            HttpResponse(status=200, content=json.dumps(page1).encode()),
            HttpResponse(status=200, content=json.dumps(page2).encode()),
        ],
    )
    ctx = ConnectorContext(http=http, credential="tok")
    items = await _all_items(ReadwiseConnector(), ctx)
    assert len(items) == 1
    doc = items[0].document
    assert doc.title == "Deep Work" and doc.author == "Cal Newport"
    assert any(b.kind is BlockKind.QUOTE and "Focus is the new IQ" in b.text for b in doc.blocks)
    assert any(b.kind is BlockKind.NOTE and "remember" in b.text for b in doc.blocks)
    # Auth header sent through the seam.
    assert _headers(http)["Authorization"] == "Token tok"


@pytest.mark.asyncio
async def test_readwise_incremental_sends_updated_after() -> None:
    http = FakeHttpClient().json_response("GET", "/export", {"results": [], "nextPageCursor": None})
    ctx = ConnectorContext(http=http, credential="tok")
    from datetime import UTC, datetime

    cursor = SyncCursor(high_watermark=datetime(2026, 1, 1, tzinfo=UTC))
    await _all_items(ReadwiseConnector(), ctx, cursor)
    assert "updatedAfter" in _params(http)


@pytest.mark.asyncio
async def test_readwise_requires_token() -> None:
    with pytest.raises(ConnectorError):
        await _all_items(ReadwiseConnector(), _ctx())


# --------------------------------------------------------------------------- #
# RSS / OPML
# --------------------------------------------------------------------------- #
_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>My Feed</title>
<item><title>Post A</title><link>http://x/a</link>
<description>&lt;p&gt;Body of post A with enough words here to render.&lt;/p&gt;</description>
<pubDate>Tue, 02 Jan 2026 10:00:00 GMT</pubDate><guid>a</guid></item>
<item><title>Post B</title><link>http://x/b</link>
<description>Plain text body B.</description>
<pubDate>Wed, 03 Jan 2026 10:00:00 GMT</pubDate><guid>b</guid></item>
</channel></rss>"""

_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Atom Post</title><id>urn:1</id>
<link href="http://x/1"/><updated>2026-01-04T10:00:00Z</updated>
<content>The atom post content has plenty of words to be a paragraph.</content>
</entry></feed>"""


@pytest.mark.asyncio
async def test_rss_parses_rss20() -> None:
    http = FakeHttpClient().add("GET", "feed", HttpResponse(status=200, content=_RSS.encode()))
    items = await _all_items(RssConnector(), _ctx(http, feed_url="http://x/feed"))
    titles = {i.document.title for i in items}
    assert titles == {"Post A", "Post B"}


@pytest.mark.asyncio
async def test_rss_parses_atom() -> None:
    http = FakeHttpClient().add("GET", "feed", HttpResponse(status=200, content=_ATOM.encode()))
    items = await _all_items(RssConnector(), _ctx(http, feed_url="http://x/feed"))
    assert items[0].document.title == "Atom Post"


@pytest.mark.asyncio
async def test_rss_etag_short_circuits() -> None:
    http = FakeHttpClient().add(
        "GET", "feed", HttpResponse(status=304, headers={"etag": "v1"})
    )
    cursor = SyncCursor(etag="v1")
    items = await _all_items(RssConnector(), _ctx(http, feed_url="http://x/feed"), cursor)
    assert items == []


@pytest.mark.asyncio
async def test_rss_incremental_filters_by_watermark() -> None:
    from datetime import UTC, datetime

    http = FakeHttpClient().add("GET", "feed", HttpResponse(status=200, content=_RSS.encode()))
    # Watermark after Post A (Jan 2) → only Post B (Jan 3) survives.
    cursor = SyncCursor(high_watermark=datetime(2026, 1, 2, 12, tzinfo=UTC))
    items = await _all_items(RssConnector(), _ctx(http, feed_url="http://x/feed"), cursor)
    assert {i.document.title for i in items} == {"Post B"}


def test_parse_opml() -> None:
    opml = """<opml><body>
    <outline text="A" xmlUrl="http://x/a.xml"/>
    <outline text="grp"><outline text="B" xmlUrl="http://x/b.xml"/></outline>
    </body></opml>"""
    assert parse_opml(opml.encode()) == ["http://x/a.xml", "http://x/b.xml"]


@pytest.mark.asyncio
async def test_rss_opml_fetches_each_feed() -> None:
    opml = '<opml><body><outline xmlUrl="http://x/a"/><outline xmlUrl="http://x/b"/></body></opml>'
    http = (
        FakeHttpClient()
        .add("GET", "http://x/a", HttpResponse(status=200, content=_RSS.encode()))
        .add("GET", "http://x/b", HttpResponse(status=200, content=_ATOM.encode()))
    )
    items = await _all_items(RssConnector(), _ctx(http, opml=opml.encode()))
    assert len(items) == 3  # 2 from RSS + 1 from Atom


# --------------------------------------------------------------------------- #
# Web article
# --------------------------------------------------------------------------- #
_ARTICLE_HTML = """<html><head><title>An Article</title></head>
<body><nav>menu home about</nav>
<article><h1>An Article</h1>
<p>This is the first paragraph of the article with quite a lot of words in it.</p>
<p>And a second paragraph that also has more than enough words to be kept here.</p>
</article><footer>copyright junk</footer>
<script>var x = 1;</script></body></html>"""


def test_article_extraction_drops_boilerplate() -> None:
    item = article_to_item("http://x/post", _ARTICLE_HTML)
    text = item.document.text()
    assert "first paragraph" in text and "second paragraph" in text
    assert "menu home" not in text and "copyright junk" not in text and "var x" not in text


def test_article_too_thin_raises_permanent() -> None:
    with pytest.raises(PermanentError):
        article_to_item("http://x/empty", "<html><body><p>hi</p></body></html>")


@pytest.mark.asyncio
async def test_web_connector_fetches_urls() -> None:
    http = FakeHttpClient().add(
        "GET", "http://x/post", HttpResponse(status=200, content=_ARTICLE_HTML.encode())
    )
    items = await _all_items(WebArticleConnector(), _ctx(http, url="http://x/post"))
    assert items[0].document.title == "An Article"


# --------------------------------------------------------------------------- #
# Pocket
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pocket_normalizes_items() -> None:
    payload = {
        "list": {
            "100": {
                "item_id": "100",
                "resolved_title": "A Saved Post",
                "resolved_url": "http://x/saved",
                "excerpt": "An excerpt of the saved article goes here.",
                "time_updated": "1735776000",
                "status": "0",
            },
            "101": {"item_id": "101", "status": "2"},  # deleted -> skipped
        }
    }
    http = FakeHttpClient().json_response("POST", "/v3/get", payload)
    ctx = ConnectorContext(http=http, credential="acc", config={"consumer_key": "ck"})
    items = await _all_items(PocketConnector(), ctx)
    assert len(items) == 1
    assert items[0].document.title == "A Saved Post"
    # Auth posted in the body, not a header.
    assert _json(http)["access_token"] == "acc"
    assert _json(http)["consumer_key"] == "ck"


@pytest.mark.asyncio
async def test_pocket_requires_consumer_key_and_token() -> None:
    with pytest.raises(ConnectorError):
        await _all_items(PocketConnector(), ConnectorContext(http=FakeHttpClient(), credential="x"))


# --------------------------------------------------------------------------- #
# Notion
# --------------------------------------------------------------------------- #
def test_notion_block_extraction() -> None:
    raw = [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Body text."}]}},
        {"type": "quote", "quote": {"rich_text": [{"plain_text": "A quote."}]}},
        {"type": "unsupported_type", "unsupported_type": {}},
        {"type": "paragraph", "paragraph": {"rich_text": []}},  # empty -> skipped
    ]
    blocks = blocks_to_normalized(raw)
    kinds = [(b.kind, b.text) for b in blocks]
    assert (BlockKind.HEADING, "Title") in kinds
    assert (BlockKind.PARAGRAPH, "Body text.") in kinds
    assert (BlockKind.QUOTE, "A quote.") in kinds
    assert len(blocks) == 3


@pytest.mark.asyncio
async def test_notion_single_page_import() -> None:
    page = {
        "id": "page1",
        "last_edited_time": "2026-01-05T00:00:00.000Z",
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "My Page"}]}},
    }
    children = {
        "results": [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello Notion."}]}}
        ],
        "has_more": False,
    }
    http = (
        FakeHttpClient()
        .json_response("GET", "/pages/page1", page)
        .json_response("GET", "/blocks/page1/children", children)
    )
    ctx = ConnectorContext(http=http, credential="ntok", config={"page_id": "page1"})
    items = await _all_items(NotionConnector(), ctx)
    assert len(items) == 1
    assert items[0].document.title == "My Page"
    assert any("Hello Notion" in b.text for b in items[0].document.blocks)
    # Version + bearer headers sent.
    assert _headers(http)["Notion-Version"]
    assert _headers(http)["Authorization"] == "Bearer ntok"


@pytest.mark.asyncio
async def test_notion_requires_target() -> None:
    ctx = ConnectorContext(http=FakeHttpClient(), credential="ntok")
    with pytest.raises(ConnectorError):
        await _all_items(NotionConnector(), ctx)
