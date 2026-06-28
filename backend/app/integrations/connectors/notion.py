"""Notion import — OAuth2 (or an internal integration token), block-tree → blocks.

Two import shapes:

* ``config['database_id']`` — query a database; each row (page) becomes a
  :class:`SourceItem`, its child block tree flattened to text blocks.
* ``config['page_id']`` — a single page becomes one :class:`SourceItem`.

The Notion API needs a ``Notion-Version`` header and a bearer token (the OAuth
``access_token`` or an internal-integration secret, passed as ``ctx.credential``).
Block extraction handles the common rich-text block types (paragraph, headings,
quotes, lists, callouts, to-dos); unknown types are skipped. Incremental sync
uses the page ``last_edited_time``.
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

_API = "https://api.notion.com/v1"
_VERSION = "2022-06-28"

#: Notion block type → our block kind. Anything not here is ignored.
_BLOCK_KIND = {
    "heading_1": BlockKind.HEADING,
    "heading_2": BlockKind.SUBHEADING,
    "heading_3": BlockKind.SUBHEADING,
    "paragraph": BlockKind.PARAGRAPH,
    "quote": BlockKind.QUOTE,
    "callout": BlockKind.NOTE,
    "bulleted_list_item": BlockKind.PARAGRAPH,
    "numbered_list_item": BlockKind.PARAGRAPH,
    "to_do": BlockKind.PARAGRAPH,
    "toggle": BlockKind.PARAGRAPH,
}


def _rich_text(items: list[dict[str, Any]] | Any) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(str(i.get("plain_text") or "") for i in items if isinstance(i, dict))


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _page_title(page: dict[str, Any]) -> str:
    """Pull the title from a page's properties (the property of type ``title``)."""
    props = page.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                title = _rich_text(prop.get("title"))
                if title:
                    return title
    return "(untitled Notion page)"


def blocks_to_normalized(raw_blocks: list[dict[str, Any]]) -> list[NormalizedBlock]:
    """Flatten a list of Notion block objects into normalized text blocks."""
    out: list[NormalizedBlock] = []
    for blk in raw_blocks:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        kind = _BLOCK_KIND.get(str(btype))
        if kind is None:
            continue
        body = blk.get(str(btype))
        if not isinstance(body, dict):
            continue
        text = _rich_text(body.get("rich_text"))
        if text.strip():
            out.append(NormalizedBlock(kind=kind, text=text))
    return out


class NotionConnector(SourceConnector):
    """Import Notion pages (single page or a queried database)."""

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="notion",
            display_name="Notion",
            capabilities=frozenset(
                {Capability.OAUTH2, Capability.TOKEN_AUTH, Capability.INCREMENTAL}
            ),
            auth_hint="Connect your Notion workspace, then pick a page or database to import.",
        )

    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        if not ctx.credential:
            raise ConnectorError("notion import requires an access token (credential)")
        page_id = ctx.cfg_str("page_id")
        database_id = ctx.cfg_str("database_id")
        if not page_id and not database_id:
            raise ConnectorError(
                "notion import requires config['page_id'] or config['database_id']"
            )
        if page_id:
            item = await self._import_page_object(ctx, page_id)
            return FetchPage(items=(item,) if item else (), next_cursor=None)
        return await self._query_database(ctx, str(database_id), cursor, page_token)

    # -- database query (paginated) ----------------------------------------- #
    async def _query_database(
        self, ctx: ConnectorContext, database_id: str, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        body: dict[str, Any] = {"page_size": 50}
        if page_token:
            body["start_cursor"] = page_token
        resp = await ctx.http.request(
            "POST",
            f"{_API}/databases/{database_id}/query",
            json=body,
            headers=self._headers(ctx),
            timeout_s=45.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ConnectorError("notion query returned an unexpected payload")
        items: list[SourceItem] = []
        for page in payload.get("results") or []:
            if not isinstance(page, dict):
                continue
            edited = _parse_iso(page.get("last_edited_time"))
            watermark = cursor.high_watermark
            if watermark is not None and edited is not None and edited <= watermark:
                continue
            item = await self._import_page_object(ctx, str(page.get("id")), page=page)
            if item is not None:
                items.append(item)
        next_cursor = payload.get("next_cursor") if payload.get("has_more") else None
        return FetchPage(items=tuple(items), next_cursor=str(next_cursor) if next_cursor else None)

    # -- a single page → item (fetch its child blocks) ---------------------- #
    async def _import_page_object(
        self, ctx: ConnectorContext, page_id: str, *, page: dict[str, Any] | None = None
    ) -> SourceItem | None:
        if page is None:
            resp = await ctx.http.request(
                "GET", f"{_API}/pages/{page_id}", headers=self._headers(ctx), timeout_s=30.0
            )
            resp.raise_for_status()
            page = resp.json() if isinstance(resp.json(), dict) else {}
        title = _page_title(page)
        edited = _parse_iso(page.get("last_edited_time"))
        raw_blocks = await self._fetch_all_blocks(ctx, page_id)
        body_blocks = blocks_to_normalized(raw_blocks)
        if not body_blocks:
            return None
        blocks = (NormalizedBlock(kind=BlockKind.HEADING, text=title), *body_blocks)
        doc = NormalizedDocument(
            title=title, author=None, blocks=blocks, metadata={"source": "notion", "id": page_id}
        )
        return SourceItem(source_id=f"notion:{page_id}", document=doc, updated_at=edited)

    async def _fetch_all_blocks(self, ctx: ConnectorContext, block_id: str) -> list[dict[str, Any]]:
        """Page through a block's children (one level; good enough for prose)."""
        out: list[dict[str, Any]] = []
        start: str | None = None
        for _ in range(50):  # hard page cap — never loop forever
            params: dict[str, Any] = {"page_size": 100}
            if start:
                params["start_cursor"] = start
            resp = await ctx.http.request(
                "GET",
                f"{_API}/blocks/{block_id}/children",
                params=params,
                headers=self._headers(ctx),
                timeout_s=30.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                break
            results = payload.get("results")
            if isinstance(results, list):
                out.extend(b for b in results if isinstance(b, dict))
            if not payload.get("has_more"):
                break
            start = payload.get("next_cursor")
            if not start:
                break
        return out

    @staticmethod
    def _headers(ctx: ConnectorContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ctx.credential}",
            "Notion-Version": _VERSION,
            "Content-Type": "application/json",
        }


__all__ = ["NotionConnector", "blocks_to_normalized"]
