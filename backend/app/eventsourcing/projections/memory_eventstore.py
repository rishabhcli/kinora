"""A deterministic in-memory :class:`EventStore` — tests + tiny embedded use.

This is the read side's stand-in for facet A's store while it is being built,
and the substrate the entire projection test-suite runs against (no infra, zero
credits). It assigns ``global_position`` and per-stream ``stream_version``
itself so a test can ``append(...)`` events and immediately project them.

Ordering is the contract every downstream computation relies on:

* ``read_all`` returns events strictly ordered by ``global_position``.
* ``read_stream`` returns one stream ordered by ``stream_version``.

The ``subscribe`` tail polls its own snapshot on a short interval and yields
freshly-appended events forever (the consumer cancels the task). Appends notify
an :class:`asyncio.Event` so a live tail wakes promptly instead of sleeping a
full interval.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime

from app.eventsourcing.projections.contracts import (
    NO_POSITION,
    GlobalPosition,
    StoredEvent,
)


class InMemoryEventStore:
    """An append-and-tail event log held in a process-local list.

    Not the production store (facet A owns that); a faithful, deterministic
    substitute that satisfies the :class:`EventStore` protocol structurally.
    """

    def __init__(self) -> None:
        self._events: list[StoredEvent] = []
        self._seen: set[str] = set()
        self._stream_versions: dict[str, int] = {}
        self._notify = asyncio.Event()

    # -- write side (test/embedded only; not part of EventStore) ------------- #

    async def append(
        self,
        stream_id: str,
        type: str,
        payload: dict[str, object] | None = None,
        *,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> StoredEvent:
        """Append one event, assigning ``global_position`` + ``stream_version``.

        Idempotent on ``event_id``: re-appending an already-seen id returns the
        existing event unchanged (mirrors a real store's dedupe key).
        """
        if event_id is not None and event_id in self._seen:
            return next(e for e in self._events if e.event_id == event_id)
        position = len(self._events) + 1
        version = self._stream_versions.get(stream_id, -1) + 1
        eid = event_id if event_id is not None else f"evt_{position:012d}"
        event = StoredEvent(
            event_id=eid,
            stream_id=stream_id,
            stream_version=version,
            global_position=position,
            type=type,
            payload=dict(payload or {}),
            recorded_at=recorded_at or datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        self._events.append(event)
        self._seen.add(eid)
        self._stream_versions[stream_id] = version
        # Wake any live tails, then reset for the next batch of appends.
        self._notify.set()
        self._notify = asyncio.Event()
        return event

    async def append_many(
        self, items: Iterable[tuple[str, str, dict[str, object]]]
    ) -> list[StoredEvent]:
        """Append a batch of ``(stream_id, type, payload)`` triples in order."""
        out: list[StoredEvent] = []
        for stream_id, type_, payload in items:
            out.append(await self.append(stream_id, type_, payload))
        return out

    # -- EventStore (read) --------------------------------------------------- #

    async def read_all(
        self,
        *,
        after_position: GlobalPosition = NO_POSITION,
        limit: int | None = None,
        types: Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        type_set = set(types) if types is not None else None
        rows = [
            e
            for e in self._events
            if e.global_position > after_position
            and (type_set is None or e.type in type_set)
        ]
        # _events is already position-ordered (append-only), but sort defensively.
        rows.sort(key=lambda e: e.global_position)
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def read_stream(
        self,
        stream_id: str,
        *,
        after_version: int = -1,
        as_of: datetime | None = None,
    ) -> list[StoredEvent]:
        rows = [
            e
            for e in self._events
            if e.stream_id == stream_id and e.stream_version > after_version
        ]
        if as_of is not None:
            rows = [e for e in rows if e.recorded_at is not None and e.recorded_at <= as_of]
        rows.sort(key=lambda e: e.stream_version)
        return rows

    async def head_position(self) -> GlobalPosition:
        return self._events[-1].global_position if self._events else NO_POSITION

    async def subscribe(
        self,
        *,
        after_position: GlobalPosition = NO_POSITION,
        poll_interval_s: float = 0.25,
    ) -> AsyncIterator[StoredEvent]:
        cursor = after_position
        while True:
            batch = await self.read_all(after_position=cursor)
            if batch:
                for event in batch:
                    cursor = event.global_position
                    yield event
                continue
            # Nothing new — wait for an append notification, but cap the wait so a
            # cancelled consumer is collected promptly.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._notify.wait(), timeout=poll_interval_s)

    # -- inspection conveniences --------------------------------------------- #

    async def count(self) -> int:
        return len(self._events)

    def all_events(self) -> list[StoredEvent]:
        return list(self._events)


__all__ = ["InMemoryEventStore"]
