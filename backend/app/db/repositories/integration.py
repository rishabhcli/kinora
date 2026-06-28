"""Repositories for the integration tables (connections, dedup ledger, runs).

These hold the queries the integrations service uses; like every repository they
*flush* but never *commit* — the unit-of-work boundary owns the transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from app.db.base import new_id
from app.db.models.integration import (
    AppConnection,
    ConnectionStatus,
    ImportedItem,
    SyncRun,
    SyncRunStatus,
)
from app.db.repositories.base import BaseRepository

#: Statuses that are not a live connection (filtered out of "list active").
_INACTIVE = (ConnectionStatus.DISCONNECTED,)


class AppConnectionRepo(BaseRepository):
    """Create, query, and mutate :class:`AppConnection` rows."""

    async def create(
        self,
        *,
        user_id: str,
        provider: str,
        status: ConnectionStatus = ConnectionStatus.PENDING,
        account_label: str | None = None,
        sealed_token: str | None = None,
        scopes: str | None = None,
        config: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> AppConnection:
        """Insert a new connection row."""
        conn = AppConnection(
            id=connection_id or new_id(),
            user_id=user_id,
            provider=provider,
            status=status,
            account_label=account_label,
            sealed_token=sealed_token,
            scopes=scopes,
            config=config or {},
        )
        self.session.add(conn)
        await self.session.flush()
        return conn

    async def get(self, connection_id: str) -> AppConnection | None:
        """Fetch a connection by id."""
        return await self.session.get(AppConnection, connection_id)

    async def get_for_user(self, connection_id: str, user_id: str) -> AppConnection | None:
        """Fetch a connection by id only if owned by ``user_id`` (authz helper)."""
        conn = await self.session.get(AppConnection, connection_id)
        return conn if conn is not None and conn.user_id == user_id else None

    async def list_for_user(
        self, user_id: str, *, include_disconnected: bool = False
    ) -> list[AppConnection]:
        """Return a user's connections, newest first."""
        stmt = select(AppConnection).where(AppConnection.user_id == user_id)
        if not include_disconnected:
            stmt = stmt.where(AppConnection.status.not_in(_INACTIVE))
        stmt = stmt.order_by(AppConnection.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_active(self, user_id: str, provider: str) -> AppConnection | None:
        """Find a user's non-disconnected connection for ``provider`` (most recent)."""
        stmt = (
            select(AppConnection)
            .where(
                AppConnection.user_id == user_id,
                AppConnection.provider == provider,
                AppConnection.status.not_in(_INACTIVE),
            )
            .order_by(AppConnection.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def update_token(
        self,
        connection_id: str,
        *,
        sealed_token: str | None,
        scopes: str | None = None,
        status: ConnectionStatus | None = None,
        account_label: str | None = None,
    ) -> None:
        """Persist a new sealed token (after authorize/refresh) + optional fields."""
        values: dict[str, Any] = {"sealed_token": sealed_token}
        if scopes is not None:
            values["scopes"] = scopes
        if status is not None:
            values["status"] = status
        if account_label is not None:
            values["account_label"] = account_label
        await self.session.execute(
            update(AppConnection).where(AppConnection.id == connection_id).values(**values)
        )
        await self.session.flush()

    async def set_status(self, connection_id: str, status: ConnectionStatus) -> None:
        """Transition a connection's status."""
        await self.session.execute(
            update(AppConnection).where(AppConnection.id == connection_id).values(status=status)
        )
        await self.session.flush()

    async def set_config(self, connection_id: str, config: dict[str, Any]) -> None:
        """Replace the connection's connector config."""
        await self.session.execute(
            update(AppConnection).where(AppConnection.id == connection_id).values(config=config)
        )
        await self.session.flush()

    async def save_cursor(
        self,
        connection_id: str,
        *,
        watermark: datetime | None,
        etag: str | None,
        opaque: str | None,
    ) -> None:
        """Persist the incremental cursor after a sync."""
        await self.session.execute(
            update(AppConnection)
            .where(AppConnection.id == connection_id)
            .values(cursor_watermark=watermark, cursor_etag=etag, cursor_opaque=opaque)
        )
        await self.session.flush()

    async def record_sync_result(
        self,
        connection_id: str,
        *,
        when: datetime,
        ok: bool,
        error: str | None,
        error_status: ConnectionStatus | None = None,
        error_threshold: int = 3,
    ) -> None:
        """Update health counters after a sync.

        On success: clear the error, zero the failure counter, mark
        ``last_synced_at``, and (if not in a terminal error/reauth state) set
        ACTIVE. On failure: bump the consecutive-failure counter; once it reaches
        ``error_threshold`` flip to ``error_status`` (ERROR by default, or
        NEEDS_REAUTH when the failure was an auth expiry).
        """
        conn = await self.session.get(AppConnection, connection_id)
        if conn is None:
            return
        if ok:
            conn.last_synced_at = when
            conn.last_error = None
            conn.consecutive_failures = 0
            if conn.status in (ConnectionStatus.PENDING, ConnectionStatus.ERROR):
                conn.status = ConnectionStatus.ACTIVE
        else:
            conn.last_error = error
            conn.consecutive_failures += 1
            if error_status is ConnectionStatus.NEEDS_REAUTH:
                conn.status = ConnectionStatus.NEEDS_REAUTH
            elif conn.consecutive_failures >= error_threshold:
                conn.status = ConnectionStatus.ERROR
        await self.session.flush()


class ImportedItemRepo(BaseRepository):
    """The dedup ledger — one row per (connection, source item)."""

    async def get(self, connection_id: str, source_item_id: str) -> ImportedItem | None:
        """Look up a ledger row by its unique (connection, source) key."""
        stmt = select(ImportedItem).where(
            ImportedItem.connection_id == connection_id,
            ImportedItem.source_item_id == source_item_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def upsert(
        self,
        *,
        connection_id: str,
        source_item_id: str,
        content_hash: str,
        book_id: str | None,
        title: str | None,
        imported_at: datetime,
    ) -> tuple[ImportedItem, bool]:
        """Create or update the ledger row; returns ``(row, created)``.

        ``created`` is True for a first import. On an update (a re-import after a
        content change) the new hash + book are recorded.
        """
        existing = await self.get(connection_id, source_item_id)
        if existing is None:
            row = ImportedItem(
                id=new_id(),
                connection_id=connection_id,
                source_item_id=source_item_id,
                content_hash=content_hash,
                book_id=book_id,
                title=title,
                imported_at=imported_at,
            )
            self.session.add(row)
            await self.session.flush()
            return row, True
        existing.content_hash = content_hash
        existing.book_id = book_id
        existing.title = title
        existing.imported_at = imported_at
        await self.session.flush()
        return existing, False

    async def list_for_connection(self, connection_id: str) -> list[ImportedItem]:
        """All ledger rows for a connection, newest first."""
        stmt = (
            select(ImportedItem)
            .where(ImportedItem.connection_id == connection_id)
            .order_by(ImportedItem.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for_connection(self, connection_id: str) -> int:
        """Number of items ever imported through a connection."""
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(ImportedItem)
            .where(ImportedItem.connection_id == connection_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())


class SyncRunRepo(BaseRepository):
    """Append-only sync-run history."""

    async def start(self, connection_id: str, *, trigger: str = "manual") -> SyncRun:
        """Open a RUNNING run row at the start of a sync."""
        run = SyncRun(
            id=new_id(),
            connection_id=connection_id,
            status=SyncRunStatus.RUNNING,
            trigger=trigger,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish(
        self,
        run_id: str,
        *,
        status: SyncRunStatus,
        seen: int,
        imported: int,
        skipped: int,
        failed: int,
        error: str | None,
        started_at: datetime | None,
        finished_at: datetime,
    ) -> None:
        """Close a run row with its final counts + status."""
        await self.session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status=status,
                items_seen=seen,
                items_imported=imported,
                items_skipped=skipped,
                items_failed=failed,
                error=error,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        await self.session.flush()

    async def get(self, run_id: str) -> SyncRun | None:
        """Fetch a run by id."""
        return await self.session.get(SyncRun, run_id)

    async def list_for_connection(self, connection_id: str, *, limit: int = 20) -> list[SyncRun]:
        """Recent runs for a connection, newest first."""
        stmt = (
            select(SyncRun)
            .where(SyncRun.connection_id == connection_id)
            .order_by(SyncRun.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["AppConnectionRepo", "ImportedItemRepo", "SyncRunRepo"]
