"""The integrations service — the one facade the API + container call.

It ties every piece together:

* the :class:`~app.integrations.registry.ConnectorRegistry` (name → connector),
* the DB repos (connections / dedup ledger / runs) over a unit-of-work factory,
* the :class:`~app.integrations.crypto.TokenSealer` (token at-rest sealing),
* the :class:`~app.integrations.oauth.OAuth2Client` builders (per provider),
* the :class:`~app.integrations.sync.SyncEngine`, and
* the :class:`~app.integrations.ingest_gateway.IngestGateway` (the §9.1 seam).

Public operations the API maps onto:

* :meth:`list_providers` — the connectable sources + their capabilities.
* :meth:`begin_oauth` / :meth:`complete_oauth` — the OAuth2 connect dance.
* :meth:`connect_token` — connect a token-auth source (Readwise) or configure a
  no-auth one (RSS feed, web URL).
* :meth:`import_file` — one-shot file-upload import (Kindle clippings, OPML).
* :meth:`sync` — run an incremental sync for a connection.
* :meth:`health` / :meth:`list_connections` — the health/status surface.
* :meth:`disconnect` — tear a connection down.

The service holds no network handle of its own; every connector call flows
through the injected :class:`~app.integrations.http.AsyncHttpClient`, so the
whole surface tests offline.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.integration import (
    AppConnection,
    ConnectionStatus,
    SyncRunStatus,
)
from app.db.repositories.integration import (
    AppConnectionRepo,
    ImportedItemRepo,
    SyncRunRepo,
)
from app.integrations.backoff import BackoffPolicy
from app.integrations.clock import Clock, SystemClock
from app.integrations.connector import Capability, ConnectorContext, ConnectorInfo
from app.integrations.crypto import TokenSealer
from app.integrations.document import render_pdf
from app.integrations.errors import (
    ConfigurationError,
    ConnectorError,
    IntegrationError,
)
from app.integrations.http import AsyncHttpClient
from app.integrations.ingest_gateway import IngestGateway
from app.integrations.models import SourceItem, SyncCursor
from app.integrations.oauth import OAuth2Client, OAuth2Config, TokenSet
from app.integrations.registry import ConnectorRegistry, default_registry
from app.integrations.sync import (
    DedupDecision,
    DedupStore,
    SyncEngine,
    SyncReport,
)

logger = get_logger("app.integrations.service")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
#: ``oauth_config(provider) -> OAuth2Config | None`` — resolve a provider's
#: OAuth2 endpoints + client credentials (from settings); ``None`` if not set up.
OAuthConfigProvider = Callable[[str], OAuth2Config | None]


@dataclass(frozen=True)
class BeginOAuthResult:
    """The output of :meth:`IntegrationsService.begin_oauth`."""

    connection_id: str
    authorize_url: str
    state: str


@dataclass
class IntegrationsService:
    """Facade over the integrations framework (constructed by the container)."""

    session_factory: SessionFactory
    ingest: IngestGateway
    http: AsyncHttpClient
    sealer: TokenSealer
    registry: ConnectorRegistry = field(default_factory=default_registry)
    oauth_config: OAuthConfigProvider | None = None
    clock: Clock = field(default_factory=SystemClock)
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    max_items_per_sync: int = 500
    error_threshold: int = 3

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def list_providers(self) -> list[ConnectorInfo]:
        """Every registered connector's descriptor (for the connect UI)."""
        return self.registry.all_info()

    # ------------------------------------------------------------------ #
    # OAuth2 connect dance
    # ------------------------------------------------------------------ #
    async def begin_oauth(self, *, user_id: str, provider: str) -> BeginOAuthResult:
        """Create a PENDING connection + hand back the authorize URL & state."""
        info = self._info(provider)
        if not info.supports(Capability.OAUTH2):
            raise ConfigurationError(f"{provider} does not support OAuth2")
        client = self._oauth_client(provider)
        url, state = client.authorize_url()
        async with self.session_factory() as db:
            conn = await AppConnectionRepo(db).create(
                user_id=user_id,
                provider=provider,
                status=ConnectionStatus.PENDING,
                config={"oauth_state": state},
            )
            connection_id = conn.id
        return BeginOAuthResult(connection_id=connection_id, authorize_url=url, state=state)

    async def complete_oauth(
        self, *, user_id: str, connection_id: str, code: str, state: str
    ) -> AppConnection:
        """Exchange the callback ``code`` for tokens and activate the connection."""
        async with self.session_factory() as db:
            repo = AppConnectionRepo(db)
            conn = await repo.get_for_user(connection_id, user_id)
            if conn is None:
                raise ConfigurationError("unknown connection")
            expected_state = conn.config.get("oauth_state")
            if not expected_state or expected_state != state:
                raise ConfigurationError("oauth state mismatch (possible CSRF)")
            client = self._oauth_client(conn.provider)
            token = await client.exchange_code(code)
            sealed = self.sealer.seal(token.as_blob())
            label = token.extra.get("workspace_name") or token.extra.get("username")
            await repo.update_token(
                connection_id,
                sealed_token=sealed,
                scopes=token.scope,
                status=ConnectionStatus.ACTIVE,
                account_label=label,
            )
            refreshed = await repo.get(connection_id)
            assert refreshed is not None
            return refreshed

    # ------------------------------------------------------------------ #
    # Token-auth / no-auth connect
    # ------------------------------------------------------------------ #
    async def connect_token(
        self,
        *,
        user_id: str,
        provider: str,
        token: str | None = None,
        config: dict[str, Any] | None = None,
        account_label: str | None = None,
    ) -> AppConnection:
        """Connect a token-auth source or configure a no-auth one.

        Readwise/Notion-internal pass ``token``; RSS/web pass only ``config``
        (feed URL / article URLs). The token, when present, is sealed at rest.
        """
        info = self._info(provider)
        sealed: str | None = None
        if token:
            sealed = self.sealer.seal(TokenSet(access_token=token).as_blob())
        elif info.supports(Capability.TOKEN_AUTH) and not info.supports(Capability.FILE_UPLOAD):
            raise ConfigurationError(f"{provider} requires a token")
        async with self.session_factory() as db:
            conn = await AppConnectionRepo(db).create(
                user_id=user_id,
                provider=provider,
                status=ConnectionStatus.ACTIVE,
                account_label=account_label,
                sealed_token=sealed,
                config=config or {},
            )
            connection_id = conn.id
            refreshed = await AppConnectionRepo(db).get(connection_id)
            assert refreshed is not None
            return refreshed

    # ------------------------------------------------------------------ #
    # File-upload import (one-shot, no connection persisted by default)
    # ------------------------------------------------------------------ #
    async def import_file(
        self,
        *,
        user_id: str,
        provider: str,
        file_bytes: bytes,
        config_key: str = "file",
    ) -> SyncReport:
        """Import an uploaded file (Kindle clippings, OPML) in one shot.

        A transient connection is created (so the dedup ledger + run history
        apply), the file is fed to the connector, and each parsed item is
        imported. Returns the :class:`SyncReport`.
        """
        info = self._info(provider)
        if not info.supports(Capability.FILE_UPLOAD):
            raise ConfigurationError(f"{provider} does not support file upload")
        async with self.session_factory() as db:
            conn = await AppConnectionRepo(db).create(
                user_id=user_id,
                provider=provider,
                status=ConnectionStatus.ACTIVE,
                config={config_key: True},  # marker; bytes are passed at fetch time
            )
            connection_id = conn.id
        return await self._run_sync(
            connection_id=connection_id,
            user_id=user_id,
            provider=provider,
            credential=None,
            extra_config={config_key: file_bytes},
            cursor=SyncCursor(),
            trigger="manual",
        )

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #
    async def sync(
        self, *, user_id: str, connection_id: str, trigger: str = "manual"
    ) -> SyncReport:
        """Run an incremental sync for a connection the user owns."""
        async with self.session_factory() as db:
            conn = await AppConnectionRepo(db).get_for_user(connection_id, user_id)
            if conn is None:
                raise ConfigurationError("unknown connection")
            provider = conn.provider
            cursor = SyncCursor(
                high_watermark=conn.cursor_watermark,
                etag=conn.cursor_etag,
                opaque=conn.cursor_opaque,
            )
            config = dict(conn.config)
            credential = await self._credential_for(db, conn)
        return await self._run_sync(
            connection_id=connection_id,
            user_id=user_id,
            provider=provider,
            credential=credential,
            extra_config=config,
            cursor=cursor,
            trigger=trigger,
        )

    async def _run_sync(
        self,
        *,
        connection_id: str,
        user_id: str,
        provider: str,
        credential: str | None,
        extra_config: dict[str, Any],
        cursor: SyncCursor,
        trigger: str,
    ) -> SyncReport:
        """The shared sync execution path (used by manual sync + file import)."""
        connector = self.registry.get(provider)
        ctx = ConnectorContext(
            http=self.http, credential=credential, config=extra_config, clock=self.clock
        )
        engine = SyncEngine(
            backoff=self.backoff, clock=self.clock, max_items=self.max_items_per_sync
        )
        dedup = _DbDedupStore(self.session_factory, connection_id)

        async def importer(item: SourceItem) -> str | None:
            return await self._import_item(user_id, provider, item)

        # Open the run row, execute, then close it + update health.
        async with self.session_factory() as db:
            run = await SyncRunRepo(db).start(connection_id, trigger=trigger)
            run_id = run.id
            started_at = run.created_at

        report = await engine.run(connector, ctx, cursor, importer, dedup)

        when = self.clock.now()
        run_status = {
            "success": SyncRunStatus.SUCCESS,
            "partial": SyncRunStatus.PARTIAL,
            "failed": SyncRunStatus.FAILED,
        }[report.status]
        ok = report.fatal_error is None and report.failed == 0
        error_status = ConnectionStatus.NEEDS_REAUTH if report.auth_expired else None
        async with self.session_factory() as db:
            await SyncRunRepo(db).finish(
                run_id,
                status=run_status,
                seen=report.seen,
                imported=report.imported,
                skipped=report.skipped,
                failed=report.failed,
                error=report.fatal_error,
                started_at=started_at,
                finished_at=when,
            )
            await AppConnectionRepo(db).save_cursor(
                connection_id,
                watermark=report.cursor.high_watermark,
                etag=report.cursor.etag,
                opaque=report.cursor.opaque,
            )
            await AppConnectionRepo(db).record_sync_result(
                connection_id,
                when=when,
                ok=ok,
                error=report.fatal_error,
                error_status=error_status,
                error_threshold=self.error_threshold,
            )
        logger.info(
            "integrations.sync.done",
            provider=provider,
            connection_id=connection_id,
            status=report.status,
            imported=report.imported,
            failed=report.failed,
        )
        return report

    async def _import_item(self, user_id: str, provider: str, item: SourceItem) -> str | None:
        """Render one item to PDF and push it through the ingest gateway."""
        doc = item.document
        if doc.is_empty():
            raise ConnectorError(f"empty document for {item.source_id}")
        pdf_bytes = render_pdf(doc)
        book_id = await self.ingest.import_pdf(
            user_id=user_id,
            title=doc.title,
            author=doc.author,
            pdf_bytes=pdf_bytes,
            source=provider,
        )
        return book_id

    # ------------------------------------------------------------------ #
    # Health / listing / disconnect
    # ------------------------------------------------------------------ #
    async def list_connections(
        self, *, user_id: str, include_disconnected: bool = False
    ) -> list[AppConnection]:
        """List a reader's connections."""
        async with self.session_factory() as db:
            return await AppConnectionRepo(db).list_for_user(
                user_id, include_disconnected=include_disconnected
            )

    async def health(
        self, *, user_id: str, connection_id: str, run_limit: int = 10
    ) -> tuple[AppConnection, int, list[Any]]:
        """Return ``(connection, imported_count, recent_runs)`` for a health view."""
        async with self.session_factory() as db:
            conn = await AppConnectionRepo(db).get_for_user(connection_id, user_id)
            if conn is None:
                raise ConfigurationError("unknown connection")
            count = await ImportedItemRepo(db).count_for_connection(connection_id)
            runs = await SyncRunRepo(db).list_for_connection(connection_id, limit=run_limit)
            return conn, count, runs

    async def disconnect(self, *, user_id: str, connection_id: str) -> None:
        """Mark a connection disconnected (kept for history)."""
        async with self.session_factory() as db:
            repo = AppConnectionRepo(db)
            conn = await repo.get_for_user(connection_id, user_id)
            if conn is None:
                raise ConfigurationError("unknown connection")
            await repo.set_status(connection_id, ConnectionStatus.DISCONNECTED)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _info(self, provider: str) -> ConnectorInfo:
        if not self.registry.has(provider):
            raise ConfigurationError(f"unknown provider: {provider!r}")
        return self.registry.info(provider)

    def _oauth_client(self, provider: str) -> OAuth2Client:
        if self.oauth_config is None:
            raise ConfigurationError("no OAuth configuration provider wired")
        config = self.oauth_config(provider)
        if config is None:
            raise ConfigurationError(f"OAuth not configured for provider {provider!r}")
        return OAuth2Client(config.require(), self.http, clock=self.clock)

    async def _credential_for(self, db: AsyncSession, conn: AppConnection) -> str | None:
        """Resolve the access credential, refreshing an expired OAuth token.

        For a token-auth source the sealed blob's ``access_token`` is returned
        directly. For an OAuth source whose access token is expired, the refresh
        token is exchanged through the OAuth client and the new sealed blob is
        persisted before returning the fresh access token.
        """
        if conn.sealed_token is None:
            return None
        try:
            blob = self.sealer.unseal(conn.sealed_token)
        except ValueError as exc:
            raise IntegrationError(f"could not read stored token: {exc}") from exc
        token = TokenSet.from_blob(blob)
        info = self.registry.info(conn.provider)
        if (
            info.supports(Capability.OAUTH2)
            and self.oauth_config is not None
            and self.oauth_config(conn.provider) is not None
            and token.is_expired(now=self.clock.now())
            and token.refresh_token
        ):
            client = self._oauth_client(conn.provider)
            token = await client.refresh(token.refresh_token)
            await AppConnectionRepo(db).update_token(
                conn.id, sealed_token=self.sealer.seal(token.as_blob()), scopes=token.scope
            )
        return token.access_token


class _DbDedupStore(DedupStore):
    """A :class:`DedupStore` backed by the ``imported_items`` ledger."""

    def __init__(self, session_factory: SessionFactory, connection_id: str) -> None:
        self._session_factory = session_factory
        self._connection_id = connection_id

    async def decide(self, item: SourceItem) -> DedupDecision:
        async with self._session_factory() as db:
            row = await ImportedItemRepo(db).get(self._connection_id, item.source_id)
        if row is None:
            return DedupDecision(should_import=True, reason="new")
        if row.content_hash != item.content_hash:
            return DedupDecision(should_import=True, reason="changed")
        return DedupDecision(should_import=False, reason="unchanged")

    async def mark_imported(self, item: SourceItem, *, book_id: str | None) -> None:
        from app.integrations.clock import SystemClock as _Clock

        async with self._session_factory() as db:
            await ImportedItemRepo(db).upsert(
                connection_id=self._connection_id,
                source_item_id=item.source_id,
                content_hash=item.content_hash,
                book_id=book_id,
                title=item.document.title,
                imported_at=_Clock().now(),
            )


__all__ = ["BeginOAuthResult", "IntegrationsService", "OAuthConfigProvider", "SessionFactory"]
