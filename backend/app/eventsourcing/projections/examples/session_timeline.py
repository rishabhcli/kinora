"""Example projection: the reading-session timeline.

Folds a reading session's event stream into one read-model row per session — the
denormalised "what happened in this session, in order" view the desktop reading
room and the analytics surface both want without re-deriving it from the raw
log every time.

Consumed event types (the command/ingest side emits these onto a
``session:<id>`` stream; this projection only declares the ones it folds):

* ``session.started`` — opens the row (book, reader, start time).
* ``session.page_viewed`` — appends a page to the path, tracks the deepest page.
* ``session.shot_played`` — counts played shots (the film side of the sync).
* ``session.director_comment`` — counts director edits (§5.4).
* ``session.stalled`` — counts buffer stalls (§4.5 buffer health).
* ``session.ended`` — closes the row (end time, duration).

The row shape is a flat JSON dict so it maps onto the KV read-model store with
no schema migration. Handlers are written to be replay-safe: counters are
recomputed from the prior row value (which, combined with the runtime's
event-id dedupe, makes the increment idempotent under at-least-once delivery).
"""

from __future__ import annotations

from typing import Any

from app.eventsourcing.projections.contracts import StoredEvent
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import ReadModelStore


class SessionTimelineProjection(Projection):
    """One read-model row per reading session: ``{session_id: {...timeline...}}``."""

    name = "session_timeline"
    version = 1

    async def _row(self, store: ReadModelStore, namespace: str, key: str) -> dict[str, Any]:
        existing = await store.get(namespace, key)
        if existing is not None:
            return existing.value
        return {
            "session_id": key,
            "book_id": None,
            "reader_id": None,
            "started_at": None,
            "ended_at": None,
            "duration_s": None,
            "pages": [],
            "deepest_page": None,
            "shots_played": 0,
            "director_comments": 0,
            "stalls": 0,
            "status": "open",
        }

    @handles("session.started")
    async def _on_started(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["book_id"] = event.payload.get("book_id")
        row["reader_id"] = event.payload.get("reader_id")
        row["started_at"] = event.payload.get("started_at") or _ts(event)
        row["status"] = "open"
        await store.put(namespace, event.stream_id, row)

    @handles("session.page_viewed")
    async def _on_page(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._row(store, namespace, event.stream_id)
        page = event.payload.get("page")
        if page is not None:
            pages: list[int] = list(row["pages"])
            pages.append(int(page))
            row["pages"] = pages
            deepest = row["deepest_page"]
            row["deepest_page"] = int(page) if deepest is None else max(int(deepest), int(page))
        await store.put(namespace, event.stream_id, row)

    @handles("session.shot_played")
    async def _on_shot(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["shots_played"] = int(row["shots_played"]) + 1
        await store.put(namespace, event.stream_id, row)

    @handles("session.director_comment")
    async def _on_comment(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["director_comments"] = int(row["director_comments"]) + 1
        await store.put(namespace, event.stream_id, row)

    @handles("session.stalled")
    async def _on_stall(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["stalls"] = int(row["stalls"]) + 1
        await store.put(namespace, event.stream_id, row)

    @handles("session.ended")
    async def _on_ended(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._row(store, namespace, event.stream_id)
        row["ended_at"] = event.payload.get("ended_at") or _ts(event)
        row["duration_s"] = event.payload.get("duration_s")
        row["status"] = "ended"
        await store.put(namespace, event.stream_id, row)


def _ts(event: StoredEvent) -> str | None:
    """ISO timestamp from the event's transaction time, if present."""
    return event.recorded_at.isoformat() if event.recorded_at is not None else None


__all__ = ["SessionTimelineProjection"]
