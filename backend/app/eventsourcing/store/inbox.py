"""The idempotent INBOX (§12.1) — Postgres-backed.

A consumer that processes a published event records ``(consumer, message_id)``
*before* (or in the same transaction as) acting on it. A redelivery of the same
message then short-circuits via :meth:`already_processed`, giving
effectively-once processing over an at-least-once transport (the outbox relay).

``message_id`` is normally the event's ``event_id`` (the dedup key the store
guarantees is unique), but any stable per-message id works. ``consumer`` scopes
the record so independent projections each track their own progress.

Like every Kinora repository this only flushes; the caller's unit of work owns
the commit — so a projection can mark-processed and write its read-model row in
one atomic step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.eventsourcing.store.models import EventStoreInbox


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PostgresInboxRepository:
    """`(consumer, message_id)` idempotency ledger over ``es_inbox``."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def already_processed(self, consumer: str, message_id: str) -> bool:
        row = (
            await self.session.execute(
                select(EventStoreInbox.message_id).where(
                    EventStoreInbox.consumer == consumer,
                    EventStoreInbox.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        return row is not None

    async def mark_processed(
        self,
        consumer: str,
        message_id: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Insert the inbox row; return ``False`` if it already existed.

        Uses ``ON CONFLICT DO NOTHING`` so two concurrent deliveries race safely:
        exactly one insert wins (``rowcount == 1``), the loser sees ``0`` and
        treats the message as already processed.
        """
        stmt = (
            pg_insert(EventStoreInbox)
            .values(
                consumer=consumer,
                message_id=message_id,
                processed_at=_utcnow(),
                result=result,
            )
            .on_conflict_do_nothing(
                index_elements=[EventStoreInbox.consumer, EventStoreInbox.message_id]
            )
        )
        # DML execute returns a CursorResult at runtime; the async stub widens it
        # to Result, so cast to read ``rowcount`` (1 = inserted, 0 = conflict).
        outcome = cast(CursorResult[Any], await self.session.execute(stmt))
        await self.session.flush()
        return bool(outcome.rowcount and outcome.rowcount > 0)


__all__ = ["PostgresInboxRepository"]
