"""Example projection: the render-shot status board (§9.7).

§9.7 is the per-shot render state machine; the render worker emits a domain
event each time a shot transitions (enqueued → rendering → QA → accepted /
rejected / degraded). This projection folds those transitions into a live status
board: one row per shot carrying its current status, attempt count, render mode,
QA score, and the last transition time — exactly what the director bar's "live
crew activity" panel and the ops board read, without polling the render queue.

It also maintains a small **summary** row (key ``__summary__``) with per-status
counts, so a dashboard can render "12 accepted / 3 rendering / 1 rejected"
without scanning every shot row. The summary is recomputed from the transition
delta (old status −1, new status +1), which is replay-safe because the runtime
dedupes already-applied events before the handler runs.

Consumed events (stream ``shot:<shot_id>``):

* ``shot.enqueued`` — first appearance; status ``queued``.
* ``shot.render_started`` — status ``rendering``; bumps attempt.
* ``shot.qa_evaluated`` — records the QA score (status unchanged).
* ``shot.accepted`` / ``shot.rejected`` / ``shot.degraded`` — terminal-ish.
"""

from __future__ import annotations

from typing import Any

from app.eventsourcing.projections.contracts import StoredEvent
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import ReadModelStore

SUMMARY_KEY = "__summary__"

#: Every status a shot row may report (the board's columns).
STATUSES = ("queued", "rendering", "accepted", "rejected", "degraded")


class ShotStatusBoardProjection(Projection):
    """One row per shot + a ``__summary__`` count row."""

    name = "shot_status_board"
    version = 1

    async def _shot_row(
        self, store: ReadModelStore, namespace: str, key: str
    ) -> dict[str, Any]:
        existing = await store.get(namespace, key)
        if existing is not None:
            return existing.value
        return {
            "shot_id": key,
            "book_id": None,
            "status": None,
            "render_mode": None,
            "attempts": 0,
            "qa_score": None,
            "updated_at": None,
        }

    async def _summary(self, store: ReadModelStore, namespace: str) -> dict[str, Any]:
        existing = await store.get(namespace, SUMMARY_KEY)
        if existing is not None:
            return existing.value
        return {"counts": dict.fromkeys(STATUSES, 0), "total": 0}

    async def _set_status(
        self,
        store: ReadModelStore,
        namespace: str,
        event: StoredEvent,
        new_status: str,
    ) -> dict[str, Any]:
        row = await self._shot_row(store, namespace, event.stream_id)
        old_status = row.get("status")
        row["status"] = new_status
        row["updated_at"] = _ts(event)
        await store.put(namespace, event.stream_id, row)
        await self._bump_summary(store, namespace, old_status, new_status)
        return row

    async def _bump_summary(
        self,
        store: ReadModelStore,
        namespace: str,
        old_status: str | None,
        new_status: str | None,
    ) -> None:
        summary = await self._summary(store, namespace)
        counts: dict[str, int] = summary["counts"]
        if old_status in counts:
            counts[old_status] = max(0, counts[old_status] - 1)
        else:
            # New shot entering the board for the first time.
            summary["total"] = int(summary["total"]) + 1
        if new_status in counts:
            counts[new_status] = counts.get(new_status, 0) + 1
        await store.put(namespace, SUMMARY_KEY, summary)

    @handles("shot.enqueued")
    async def _on_enqueued(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._set_status(store, namespace, event, "queued")
        row["book_id"] = event.payload.get("book_id")
        row["render_mode"] = event.payload.get("render_mode")
        await store.put(namespace, event.stream_id, row)

    @handles("shot.render_started")
    async def _on_started(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        row = await self._set_status(store, namespace, event, "rendering")
        row["attempts"] = int(row["attempts"]) + 1
        mode = event.payload.get("render_mode")
        if mode is not None:
            row["render_mode"] = mode
        await store.put(namespace, event.stream_id, row)

    @handles("shot.qa_evaluated")
    async def _on_qa(self, store: ReadModelStore, namespace: str, event: StoredEvent) -> None:
        row = await self._shot_row(store, namespace, event.stream_id)
        row["qa_score"] = event.payload.get("score")
        row["updated_at"] = _ts(event)
        await store.put(namespace, event.stream_id, row)

    @handles("shot.accepted")
    async def _on_accepted(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await self._set_status(store, namespace, event, "accepted")

    @handles("shot.rejected")
    async def _on_rejected(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await self._set_status(store, namespace, event, "rejected")

    @handles("shot.degraded")
    async def _on_degraded(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await self._set_status(store, namespace, event, "degraded")


def _ts(event: StoredEvent) -> str | None:
    return event.recorded_at.isoformat() if event.recorded_at is not None else None


__all__ = ["SUMMARY_KEY", "ShotStatusBoardProjection"]
