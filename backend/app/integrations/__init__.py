"""Third-party integrations & import framework (backend/app/integrations/).

Bring reading material in from where the reader already keeps it — Readwise,
Kindle exports, Notion, RSS/OPML, Pocket, plain web articles — and turn each
source item into a Kinora book that flows through the *unchanged* §9.1 Phase-A
ingest pipeline.

Layering (low → high):

* :mod:`.errors`, :mod:`.models` — the error hierarchy + the normalized format.
* :mod:`.document`, :mod:`.htmlutil` — render a normalized doc → ingest PDF.
* :mod:`.http`, :mod:`.clock`, :mod:`.backoff`, :mod:`.crypto` — the seams.
* :mod:`.connector`, :mod:`.registry`, :mod:`.oauth` — the abstraction.
* :mod:`.connectors` — the concrete sources.
* :mod:`.sync`, :mod:`.webhooks`, :mod:`.health` — the engine + surfaces.
* :mod:`.service` — the facade the API/container call.

Hard rule honoured throughout: the only thing that can touch the network is the
injected :class:`~app.integrations.http.AsyncHttpClient`; tests replace it.
"""

from __future__ import annotations

from app.integrations.connector import (
    Capability,
    ConnectorContext,
    ConnectorInfo,
    SourceConnector,
)
from app.integrations.errors import (
    AuthExpired,
    ConfigurationError,
    ConnectorError,
    IntegrationError,
    PermanentError,
    RateLimited,
    TransientError,
)
from app.integrations.models import (
    BlockKind,
    FetchPage,
    NormalizedBlock,
    NormalizedDocument,
    SourceItem,
    SyncCursor,
)
from app.integrations.registry import ConnectorRegistry, default_registry

__all__ = [
    "AuthExpired",
    "BlockKind",
    "Capability",
    "ConfigurationError",
    "ConnectorContext",
    "ConnectorError",
    "ConnectorInfo",
    "ConnectorRegistry",
    "FetchPage",
    "IntegrationError",
    "NormalizedBlock",
    "NormalizedDocument",
    "PermanentError",
    "RateLimited",
    "SourceConnector",
    "SourceItem",
    "SyncCursor",
    "TransientError",
    "default_registry",
]
