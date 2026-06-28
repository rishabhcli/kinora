"""Source connectors — one module per import source.

Each connector subclasses :class:`app.integrations.connector.SourceConnector`
and normalizes its source into :class:`app.integrations.models.SourceItem`s. The
:func:`app.integrations.registry.default_registry` registers them all.
"""

from __future__ import annotations

from app.integrations.connectors.kindle import KindleClippingsConnector
from app.integrations.connectors.notion import NotionConnector
from app.integrations.connectors.pocket import PocketConnector
from app.integrations.connectors.readwise import ReadwiseConnector
from app.integrations.connectors.rss import RssConnector
from app.integrations.connectors.web import WebArticleConnector

__all__ = [
    "KindleClippingsConnector",
    "NotionConnector",
    "PocketConnector",
    "ReadwiseConnector",
    "RssConnector",
    "WebArticleConnector",
]
