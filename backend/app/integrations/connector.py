"""The connector abstraction every source plugs into.

A :class:`SourceConnector` is a small, stateless adapter: given a
:class:`ConnectorContext` (credentials + the network seam + a clock) and an
optional incremental :class:`~app.integrations.models.SyncCursor`, it yields
pages of :class:`~app.integrations.models.SourceItem`. It declares its
:class:`Capability` set (token vs OAuth auth, incremental, webhook, file-upload)
so the service/UI know how to drive it. Connectors hold no DB handles and never
touch object storage — they only produce normalized content.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum

from app.integrations.clock import Clock, SystemClock
from app.integrations.http import AsyncHttpClient
from app.integrations.models import FetchPage, SourceItem, SyncCursor


class Capability(StrEnum):
    """What a connector supports (so the service/UI can drive it correctly)."""

    #: Authenticates with a static API token / personal access token.
    TOKEN_AUTH = "token_auth"
    #: Authenticates via the OAuth2 authorization-code flow (refreshable).
    OAUTH2 = "oauth2"
    #: Supports incremental sync via a cursor / high-watermark.
    INCREMENTAL = "incremental"
    #: Can receive push notifications via a webhook.
    WEBHOOK = "webhook"
    #: Imports from an uploaded file (no network), e.g. Kindle clippings / OPML.
    FILE_UPLOAD = "file_upload"


@dataclass(frozen=True)
class ConnectorInfo:
    """Static descriptor of a connector for the connect/health surface."""

    name: str
    display_name: str
    capabilities: frozenset[Capability]
    #: Human note shown in the connect dialog (what to paste, where to find it).
    auth_hint: str = ""

    def supports(self, capability: Capability) -> bool:
        """Whether this connector declares ``capability``."""
        return capability in self.capabilities


@dataclass
class ConnectorContext:
    """Everything a connector needs at fetch time — and nothing it doesn't.

    The credential is whatever the connector's auth model uses: a static token
    string for :class:`Capability.TOKEN_AUTH`, or the OAuth ``access_token`` for
    :class:`Capability.OAUTH2`. ``config`` carries connector-specific options
    (a feed URL, a Notion database id, an uploaded file's bytes, etc.).
    """

    http: AsyncHttpClient
    credential: str | None = None
    config: dict[str, object] = field(default_factory=dict)
    clock: Clock = field(default_factory=SystemClock)

    def cfg_str(self, key: str, default: str | None = None) -> str | None:
        """Read a string option from ``config`` (``None`` if absent/wrong type)."""
        value = self.config.get(key, default)
        return value if isinstance(value, str) else default

    def cfg_bytes(self, key: str) -> bytes | None:
        """Read uploaded ``bytes`` from ``config`` (for file-upload connectors)."""
        value = self.config.get(key)
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        return None


class SourceConnector(abc.ABC):
    """Base class for a source connector.

    Subclasses implement :meth:`info` (static descriptor) and :meth:`fetch_page`
    (one page of items given a cursor). The default :meth:`iter_items` drives
    pagination; the sync engine calls that. A connector is cheap to construct and
    holds no mutable per-run state.
    """

    @classmethod
    @abc.abstractmethod
    def info(cls) -> ConnectorInfo:
        """The static descriptor for this connector."""

    @abc.abstractmethod
    async def fetch_page(
        self, ctx: ConnectorContext, cursor: SyncCursor, page_token: str | None
    ) -> FetchPage:
        """Fetch one page of items.

        Args:
            ctx: credentials + network seam + clock.
            cursor: the persisted incremental state (filter to items after its
                ``high_watermark``); a connector without incremental support may
                ignore it.
            page_token: the opaque pagination token from the previous page's
                ``FetchPage.next_cursor`` (``None`` for the first page).

        Returns:
            A :class:`FetchPage`; ``next_cursor=None`` ends pagination.
        """

    async def iter_items(
        self, ctx: ConnectorContext, cursor: SyncCursor
    ) -> AsyncIterator[SourceItem]:
        """Yield every item across all pages (the sync-engine entry point).

        Walks pagination via ``fetch_page``; a connector that returns an ``etag``
        equal to ``cursor.etag`` on its first page yields nothing (unchanged feed).
        """
        page_token: str | None = None
        first = True
        while True:
            page = await self.fetch_page(ctx, cursor, page_token)
            if first and page.etag is not None and page.etag == cursor.etag:
                return  # unchanged since last sync — short-circuit
            first = False
            for item in page.items:
                yield item
            if page.next_cursor is None:
                return
            page_token = page.next_cursor

    def page_etag(self, page: FetchPage) -> str | None:
        """Expose a page's etag for the sync engine's cursor persistence."""
        return page.etag


__all__ = [
    "Capability",
    "ConnectorContext",
    "ConnectorInfo",
    "SourceConnector",
]
