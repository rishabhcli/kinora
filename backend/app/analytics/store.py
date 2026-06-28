"""The analytics store seam + a deterministic in-memory implementation.

:class:`AnalyticsStore` is the single persistence seam every analysis runs over.
:class:`InMemoryAnalyticsStore` is the deterministic, dependency-free
implementation used by the bulk of the test-suite (and viable as a tiny embedded
backend); the Postgres-backed store lives in :mod:`app.analytics.store_pg`.

Idempotency is the contract: :meth:`AnalyticsStore.append` dedupes on
``event_id`` so a retried ingest batch is a no-op for already-seen events and
returns how many rows were *newly* inserted. Reads return events in a stable
order (``occurred_at`` then ``event_id``) so every downstream computation is
deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.analytics.events import EventName, TrackedEvent


@runtime_checkable
class AnalyticsStore(Protocol):
    """Append-and-query persistence for scrubbed analytics events."""

    async def append(self, events: Iterable[TrackedEvent]) -> int:
        """Idempotently append ``events`` (dedupe on ``event_id``); return new count."""
        ...

    async def query(
        self,
        *,
        names: Sequence[EventName] | None = None,
        book_id: str | None = None,
        anon_user_id: str | None = None,
        session_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[TrackedEvent]:
        """Return matching events ordered by ``(occurred_at, event_id)``."""
        ...

    async def count(self) -> int:
        """Total number of stored events."""
        ...


def event_sort_key(event: TrackedEvent) -> tuple[datetime, str]:
    """Stable total order: by ``occurred_at`` then ``event_id`` as a tiebreak."""
    return (event.occurred_at, event.event_id)


def matches(
    event: TrackedEvent,
    *,
    names: Sequence[EventName] | None,
    book_id: str | None,
    anon_user_id: str | None,
    session_key: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    """Predicate shared by every store implementation (pure)."""
    if names is not None and event.name not in names:
        return False
    if book_id is not None and event.book_id != book_id:
        return False
    if anon_user_id is not None and event.anon_user_id != anon_user_id:
        return False
    if session_key is not None and event.session_key != session_key:
        return False
    if since is not None and event.occurred_at < since:
        return False
    return not (until is not None and event.occurred_at >= until)


class InMemoryAnalyticsStore:
    """A deterministic, in-process :class:`AnalyticsStore` (tests / embedded use).

    Events are held in insertion order in a list with an ``event_id`` index for
    O(1) dedupe. Reads filter + sort into a stable order, so results never depend
    on insertion order or dict iteration order.
    """

    def __init__(self) -> None:
        self._events: list[TrackedEvent] = []
        self._seen: set[str] = set()

    async def append(self, events: Iterable[TrackedEvent]) -> int:
        new = 0
        for event in events:
            if event.event_id in self._seen:
                continue
            self._seen.add(event.event_id)
            self._events.append(event)
            new += 1
        return new

    async def query(
        self,
        *,
        names: Sequence[EventName] | None = None,
        book_id: str | None = None,
        anon_user_id: str | None = None,
        session_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[TrackedEvent]:
        rows = [
            e
            for e in self._events
            if matches(
                e,
                names=names,
                book_id=book_id,
                anon_user_id=anon_user_id,
                session_key=session_key,
                since=since,
                until=until,
            )
        ]
        rows.sort(key=event_sort_key)
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def count(self) -> int:
        return len(self._events)

    # -- test/inspection conveniences (not part of the protocol) ------------- #

    def all_events(self) -> list[TrackedEvent]:
        """A stable-sorted snapshot of every stored event."""
        return sorted(self._events, key=event_sort_key)

    def clear(self) -> None:
        """Drop all stored events (test reset)."""
        self._events.clear()
        self._seen.clear()


__all__ = [
    "AnalyticsStore",
    "InMemoryAnalyticsStore",
    "event_sort_key",
    "matches",
]
