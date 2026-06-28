"""Connection-health + sync-status projections for the UI surface.

The reader's settings panel shows a row per connection: is it healthy, when did
it last sync, how many items has it imported, what went wrong last time, and the
recent run history. This module turns the DB rows
(:class:`~app.db.models.integration.AppConnection`,
:class:`~app.db.models.integration.SyncRun`) into small, JSON-able value objects
the API serialises. Pure projection — no I/O, no DB access — so it is trivially
testable and reusable by any surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.db.models.integration import AppConnection, ConnectionStatus, SyncRun


@dataclass(frozen=True)
class SyncRunView:
    """A single run row projected for the UI."""

    id: str
    status: str
    trigger: str
    seen: int
    imported: int
    skipped: int
    failed: int
    error: str | None
    started_at: str | None
    finished_at: str | None

    @classmethod
    def of(cls, run: SyncRun) -> SyncRunView:
        """Project a :class:`SyncRun` row."""
        return cls(
            id=run.id,
            status=run.status.value,
            trigger=run.trigger,
            seen=run.items_seen,
            imported=run.items_imported,
            skipped=run.items_skipped,
            failed=run.items_failed,
            error=run.error,
            started_at=_iso(run.started_at),
            finished_at=_iso(run.finished_at),
        )


@dataclass(frozen=True)
class ConnectionHealth:
    """A connection's health + status, projected for the UI."""

    id: str
    provider: str
    account_label: str | None
    status: str
    #: A coarse traffic-light derived from status + failure counter.
    health: str  # "healthy" | "degraded" | "down" | "needs_attention"
    last_synced_at: str | None
    last_error: str | None
    consecutive_failures: int
    imported_count: int
    needs_reauth: bool
    recent_runs: list[SyncRunView] = field(default_factory=list)

    @classmethod
    def of(
        cls,
        conn: AppConnection,
        *,
        imported_count: int = 0,
        recent_runs: list[SyncRun] | None = None,
    ) -> ConnectionHealth:
        """Project a connection (+ optional run history) into a health view."""
        return cls(
            id=conn.id,
            provider=conn.provider,
            account_label=conn.account_label,
            status=conn.status.value,
            health=_health_of(conn),
            last_synced_at=_iso(conn.last_synced_at),
            last_error=conn.last_error,
            consecutive_failures=conn.consecutive_failures,
            imported_count=imported_count,
            needs_reauth=conn.status is ConnectionStatus.NEEDS_REAUTH,
            recent_runs=[SyncRunView.of(r) for r in (recent_runs or [])],
        )


def _health_of(conn: AppConnection) -> str:
    """Map status + failure counter to a coarse traffic-light value."""
    if conn.status is ConnectionStatus.NEEDS_REAUTH:
        return "needs_attention"
    if conn.status is ConnectionStatus.ERROR:
        return "down"
    if conn.status is ConnectionStatus.DISCONNECTED:
        return "down"
    if conn.consecutive_failures > 0:
        return "degraded"
    if conn.status is ConnectionStatus.PENDING:
        return "needs_attention"
    return "healthy"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["ConnectionHealth", "SyncRunView"]
