"""Gap-free global-position allocation.

A Postgres ``SEQUENCE``/``SERIAL`` is *not* gap-free: a number handed out to a
transaction that later rolls back is gone forever, leaving a hole. A catch-up
projection that pages "everything with ``global_position`` ≤ P" cannot then trust
that it has seen every event ≤ P without a tracking-gap window.

This allocator gives a **dense, gap-free** sequence by keeping the counter in a
single row (``es_sequence``) and bumping it inside the *same* transaction as the
append, under a row lock (``SELECT ... FOR UPDATE``). If the append rolls back,
the bump rolls back with it and the numbers are returned — no holes. The cost is
that concurrent appends serialise on this one row; an accepted trade for a clean
global order at this volume (canon / scheduler / render facts). The lock is held
only for the duration of the append transaction.

Isolated here so a future high-throughput variant (e.g. hash-partitioned counters
with a merge view) is a localised change.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.eventsourcing.store.models import GLOBAL_SEQUENCE_NAME, EventStoreSequence


async def _ensure_row(session: AsyncSession, name: str) -> None:
    """Insert the counter row if it is missing (idempotent, race-safe).

    Uses an ``ON CONFLICT DO NOTHING`` upsert so two concurrent allocators racing
    to create the row don't error; whichever loses simply re-reads it.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(EventStoreSequence)
        .values(name=name, value=0)
        .on_conflict_do_nothing(index_elements=[EventStoreSequence.name])
    )
    await session.execute(stmt)


async def allocate(session: AsyncSession, count: int, *, name: str = GLOBAL_SEQUENCE_NAME) -> int:
    """Allocate ``count`` consecutive global positions, returning the *first*.

    The returned value ``start`` means the allocated positions are
    ``start, start+1, …, start+count-1``. Must be called inside the append's
    transaction; the row lock serialises concurrent allocations and the bump is
    rolled back with the transaction on failure (keeping the sequence gap-free).
    """
    if count <= 0:
        raise ValueError("count must be >= 1")

    await _ensure_row(session, name)

    # Lock the counter row for the rest of the transaction.
    locked = (
        select(EventStoreSequence.value)
        .where(EventStoreSequence.name == name)
        .with_for_update()
    )
    current = (await session.execute(locked)).scalar_one()
    start = current + 1
    new_value = current + count
    await session.execute(
        update(EventStoreSequence)
        .where(EventStoreSequence.name == name)
        .values(value=new_value)
    )
    return start


async def current_value(session: AsyncSession, *, name: str = GLOBAL_SEQUENCE_NAME) -> int:
    """The last allocated value (0 if never allocated). No lock taken."""
    row = (
        await session.execute(
            select(EventStoreSequence.value).where(EventStoreSequence.name == name)
        )
    ).scalar_one_or_none()
    return int(row or 0)


__all__ = ["allocate", "current_value"]
