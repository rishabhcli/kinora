"""Plain web-article extraction — turn a URL into a readable book.

Given one or more ``config['urls']`` (or a single ``config['url']``), this
connector fetches each page through the injected HTTP seam and runs the stdlib
readability extractor (:mod:`app.integrations.htmlutil`) to pull the title and
prose, dropping nav/script/footer boilerplate. Each URL becomes one
:class:`SourceItem`. There is no auth and (by default) no incremental cursor: a
web URL is a one-shot import unless re-run.
"""

from __future__ import annotations

import hashlib

from app.integrations.connector import (
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

#: Articles thinner than this many words are almost certainly a paywall stub /
#: cookie-wall / JS shell — surface a permanent error so the caller skips them.
_MIN_ARTICLE_WORDS = 25


def article_to_item(url: str, html: str) -> SourceItem:
    """Extract a readable :class:`SourceItem` from one page's HTML.

    Raises:
        PermanentError: when the page yields too little prose to be a real article
            (paywall/JS-only page). Permanent so the sync engine does not retry.
    """
    extracted = extract_article(html)
    if extracted.word_count() < _MIN_ARTICLE_WORDS:
        raise PermanentError(f"no extractable article content at {url}")
    title = extracted.title or url
    blocks: list[NormalizedBlock] = []
    for b in extracted.blocks:
        kind = {
            "heading": BlockKind.HEADING,
            "subheading": BlockKind.SUBHEADING,
            "quote": BlockKind.QUOTE,
        }.get(b.role, BlockKind.PARAGRAPH)
        blocks.append(NormalizedBlock(kind=kind, text=b.text))
    doc = NormalizedDocument(
        title=title,
        author=None,
        blocks=tuple(blocks),
        metadata={"source": "web", "url": url},
    )
    source_id = "web:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return SourceItem(source_id=source_id, document=doc, external_ref=url)


class WebArticleConnector(SourceConnector):
    """Import one or more web articles by URL (readability extraction)."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="web",
            display_name="Web Article",
            capabilities=frozenset(),
            auth_hint="Paste an article URL (or a list of URLs).",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        urls = self._urls(ctx)
        if not urls:
            raise ConnectorError("web import requires config['url'] or config['urls']")
        items: list[SourceItem] = []
        for url in urls:
            resp = await ctx.http.request("GET", url, timeout_s=30.0)
            resp.raise_for_status()
            items.append(article_to_item(resp.url or url, resp.text))
        return FetchPage(items=tuple(items), next_cursor=None)

    @staticmethod
    def _urls(ctx: ConnectorContext) -> list[str]:
        many = ctx.config.get("urls")
        if isinstance(many, (list, tuple)):
            return [str(u) for u in many if isinstance(u, str) and u.strip()]
        single = ctx.cfg_str("url")
        return [single] if single else []


__all__ = ["WebArticleConnector", "article_to_item"]
