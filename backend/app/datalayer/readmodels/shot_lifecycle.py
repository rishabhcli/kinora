"""Per-shot §9.7 lifecycle board (folds the render-shot stream, one row per shot).

Where :mod:`app.datalayer.readmodels.render_progress` aggregates *per book*, this
projection keeps the fine-grained per-shot board the director bar's "live crew
activity" panel reads: one row per ``shot_id`` carrying its current §9.7 state,
attempt count, latest QA score, video-seconds, and the last transition reason +
time — without polling the render queue.

It maintains a small ``__summary__`` row with per-state counts so a dashboard can
render "12 accepted / 3 rendering / 1 degraded" without scanning every shot. The
summary is recomputed from the transition delta (old state -1, new state +1),
which is replay-safe because the runner dedupes already-applied events before the
handler runs.

Consumed event types: ``ShotPlanned`` (genesis), ``ShotTransitioned`` (the
general §9.7 edge, carries ``to_state``), ``ShotKeyframed``, ``ShotRendered``,
``ShotQAScored``, ``ShotAccepted``, ``ShotDegraded``, ``ShotConflictRaised``,
``ShotRegenRequested``.
"""

from __future__ import annotations

from typing import Any

from app.datalayer.envelope import ProjectionEvent
from app.datalayer.projector import Projection, handles
from app.datalayer.readmodel import ReadModelStore

SUMMARY_KEY = "__summary__"


class ShotLifecycleProjection(Projection):
    """One row per ``shot_id`` + a ``__summary__`` per-state count row."""

    name = "shot_lifecycle"
    version = 1

    @staticmethod
    def _shot_id(event: ProjectionEvent) -> str | None:
        sid = event.data.get("shot_id")
        if isinstance(sid, str) and sid:
            return sid
        if "-" in event.stream_id:
            return event.stream_id.split("-", 1)[1]
        return event.stream_id or None

    async def _shot_row(
        self, store: ReadModelStore, namespace: str, shot_id: str
    ) -> dict[str, Any]:
        existing = await store.get(namespace, shot_id)
        if existing is not None:
            return existing.value
        return {
            "shot_id": shot_id,
            "book_id": None,
            "state": None,
            "attempts": 0,
            "qa_score": None,
            "qa_passed": None,
            "video_seconds": 0.0,
            "keyframe_url": None,
            "clip_url": None,
            "last_reason": None,
            "updated_at": None,
        }

    async def _summary(self, store: ReadModelStore, namespace: str) -> dict[str, Any]:
        existing = await store.get(namespace, SUMMARY_KEY)
        if existing is not None:
            return existing.value
        return {"counts": {}, "total": 0}

    async def _set_state(
        self,
        store: ReadModelStore,
        namespace: str,
        event: ProjectionEvent,
        new_state: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        shot_id = self._shot_id(event)
        assert shot_id is not None  # callers guard before invoking
        row = await self._shot_row(store, namespace, shot_id)
        old_state = row.get("state")
        row["state"] = new_state
        if reason is not None:
            row["last_reason"] = reason
        row["updated_at"] = _ts(event)
        await store.put(namespace, shot_id, row)
        await self._bump_summary(store, namespace, old_state, new_state)
        return row

    async def _bump_summary(
        self,
        store: ReadModelStore,
        namespace: str,
        old_state: str | None,
        new_state: str | None,
    ) -> None:
        summary = await self._summary(store, namespace)
        counts: dict[str, int] = summary["counts"]
        if old_state is None:
            summary["total"] = int(summary["total"]) + 1
        elif old_state in counts:
            counts[old_state] = max(0, counts[old_state] - 1)
        if new_state is not None:
            counts[new_state] = counts.get(new_state, 0) + 1
        await store.put(namespace, SUMMARY_KEY, summary)

    @handles("ShotPlanned")
    async def _on_planned(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row = await self._set_state(store, namespace, event, "Planned", reason="planned")
        row["book_id"] = event.data.get("book_id") or row["book_id"]
        await store.put(namespace, shot_id, row)

    @handles("ShotTransitioned")
    async def _on_transitioned(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        if self._shot_id(event) is None:
            return
        to_state = event.data.get("to_state")
        if not isinstance(to_state, str) or not to_state:
            return
        reason = event.data.get("reason")
        await self._set_state(
            store, namespace, event, to_state, reason=reason if isinstance(reason, str) else None
        )

    @handles("ShotKeyframed")
    async def _on_keyframed(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row = await self._shot_row(store, namespace, shot_id)
        row["keyframe_url"] = event.data.get("keyframe_url") or row["keyframe_url"]
        row["updated_at"] = _ts(event)
        await store.put(namespace, shot_id, row)

    @handles("ShotRendered")
    async def _on_rendered(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row = await self._shot_row(store, namespace, shot_id)
        # attempts counts renders; replay-safe via the runner's event-id dedupe.
        row["attempts"] = int(row["attempts"]) + 1
        row["video_seconds"] = _as_float(event.data.get("video_seconds"))
        row["clip_url"] = event.data.get("clip_url") or row["clip_url"]
        row["updated_at"] = _ts(event)
        await store.put(namespace, shot_id, row)

    @handles("ShotQAScored")
    async def _on_qa(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row = await self._shot_row(store, namespace, shot_id)
        row["qa_score"] = event.data.get("score")
        row["qa_passed"] = event.data.get("passed")
        row["updated_at"] = _ts(event)
        await store.put(namespace, shot_id, row)

    @handles("ShotAccepted")
    async def _on_accepted(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        if self._shot_id(event) is None:
            return
        row = await self._set_state(store, namespace, event, "Accepted", reason="accepted")
        clip = event.data.get("clip_url")
        if clip:
            row["clip_url"] = clip
            await store.put(namespace, row["shot_id"], row)

    @handles("ShotDegraded")
    async def _on_degraded(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        if self._shot_id(event) is None:
            return
        reason = event.data.get("reason")
        await self._set_state(
            store,
            namespace,
            event,
            "Degraded",
            reason=reason if isinstance(reason, str) else "degraded",
        )

    @handles("ShotConflictRaised")
    async def _on_conflict(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        if self._shot_id(event) is None:
            return
        detail = event.data.get("detail")
        await self._set_state(
            store,
            namespace,
            event,
            "Conflict",
            reason=detail if isinstance(detail, str) else "conflict",
        )

    @handles("ShotRegenRequested")
    async def _on_regen(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        if self._shot_id(event) is None:
            return
        reason = event.data.get("reason")
        await self._set_state(
            store,
            namespace,
            event,
            "Promoted",
            reason=reason if isinstance(reason, str) else "regen",
        )


class ShotLifecycleRepository:
    """Read facade over the ``shot_lifecycle`` namespace."""

    def __init__(self, store: ReadModelStore, *, namespace: str = "shot_lifecycle") -> None:
        self._store = store
        self._namespace = namespace

    async def for_shot(self, shot_id: str) -> dict[str, Any] | None:
        row = await self._store.get(self._namespace, shot_id)
        return row.value if row is not None and shot_id != SUMMARY_KEY else None

    async def board(self) -> list[dict[str, Any]]:
        """Every shot row (excluding the summary), ordered by ``shot_id``."""
        return [
            r.value for r in await self._store.list(self._namespace) if r.key != SUMMARY_KEY
        ]

    async def summary(self) -> dict[str, Any]:
        """The per-state count summary (``{"counts": {...}, "total": N}``)."""
        row = await self._store.get(self._namespace, SUMMARY_KEY)
        return row.value if row is not None else {"counts": {}, "total": 0}

    async def in_state(self, state: str) -> list[dict[str, Any]]:
        return [r for r in await self.board() if r.get("state") == state]


def _ts(event: ProjectionEvent) -> str | None:
    return event.recorded_at.isoformat() if event.recorded_at is not None else None


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


__all__ = ["SUMMARY_KEY", "ShotLifecycleProjection", "ShotLifecycleRepository"]
