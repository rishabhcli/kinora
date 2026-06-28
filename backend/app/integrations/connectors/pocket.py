"""Pocket saved-articles import — OAuth-style consumer/access auth, incremental.

Pocket's retrieve API (``POST /v3/get``) returns the user's saved items; each
saved item becomes a :class:`SourceItem` built from its title + excerpt (Pocket
does not return full article body, so the excerpt is the content, and the URL is
carried as provenance for a later full-text fetch by the web connector).

Pocket auth is its own consumer-key + access-token pair rather than a bearer
header: both are posted in the request body. The container supplies the
``consumer_key`` via ``ctx.config['consumer_key']`` and the per-user access token
via ``ctx.credential``. Incremental sync uses the ``since`` Unix-timestamp cursor
Pocket echoes back.
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

_GET_URL = "https://getpocket.com/v3/get"
_PAGE_SIZE = 30


def _ts(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _item_to_source(raw: dict[str, Any]) -> SourceItem | None:
    item_id = str(raw.get("item_id") or raw.get("resolved_id") or "")
    if not item_id:
        return None
    title = str(raw.get("resolved_title") or raw.get("given_title") or "").strip()
    url = str(raw.get("resolved_url") or raw.get("given_url") or "").strip()
    if not title:
        title = url or "(untitled Pocket item)"
    excerpt = str(raw.get("excerpt") or "").strip()
    blocks: list[NormalizedBlock] = [NormalizedBlock(kind=BlockKind.HEADING, text=title)]
    if url:
        blocks.append(NormalizedBlock(kind=BlockKind.NOTE, text=url, cite="saved from"))
    if excerpt:
        blocks.append(NormalizedBlock(kind=BlockKind.PARAGRAPH, text=excerpt))
    if len(blocks) == 1:
        return None
    updated = _ts(raw.get("time_updated")) or _ts(raw.get("time_added"))
    doc = NormalizedDocument(
        title=title,
        author=None,
        blocks=tuple(blocks),
        metadata={"source": "pocket", "url": url, "item_id": item_id},
    )
    return SourceItem(
        source_id=f"pocket:{item_id}", document=doc, updated_at=updated, external_ref=url
    )


class PocketConnector(SourceConnector):
    """Import Pocket saved articles (title + excerpt, URL kept for provenance)."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="pocket",
            display_name="Pocket",
            capabilities=frozenset({Capability.OAUTH2, Capability.INCREMENTAL}),
            auth_hint="Authorize Kinora to read your Pocket list.",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        consumer_key = ctx.cfg_str("consumer_key")
        if not consumer_key:
            raise ConnectorError("pocket import requires config['consumer_key']")
        if not ctx.credential:
            raise ConnectorError("pocket import requires an access token (credential)")
        offset = int(page_token) if page_token and page_token.isdigit() else 0
        body: dict[str, Any] = {
            "consumer_key": consumer_key,
            "access_token": ctx.credential,
            "detailType": "complete",
            "sort": "newest",
            "count": _PAGE_SIZE,
            "offset": offset,
        }
        if cursor.high_watermark is not None:
            body["since"] = int(cursor.high_watermark.timestamp())

        resp = await ctx.http.request(
            "POST", _GET_URL, json=body, headers={"Accept": "application/json"}, timeout_s=45.0
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ConnectorError("pocket retrieve returned an unexpected payload")

        raw_list = payload.get("list")
        records = list(raw_list.values()) if isinstance(raw_list, dict) else []
        items: list[SourceItem] = []
        for raw in records:
            if isinstance(raw, dict) and str(raw.get("status")) != "2":  # 2 == deleted
                item = _item_to_source(raw)
                if item is not None:
                    items.append(item)
        # Pocket has more if it returned a full page.
        next_cursor = str(offset + _PAGE_SIZE) if len(records) >= _PAGE_SIZE else None
        return FetchPage(items=tuple(items), next_cursor=next_cursor)


__all__ = ["PocketConnector"]
