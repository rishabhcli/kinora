"""Per-book render-progress read model (folds the §9.7 render-shot stream).

The render-shot aggregate (:mod:`app.eventsourcing.domain.render_shot`) emits a
domain event per §9.7 transition. This projection folds those, keyed by
``book_id``, into a progress row the library card + reading-room buffer read
without scanning the render queue:

* ``shots_planned`` — shots that entered the shot list (genesis ``ShotPlanned``);
* ``shots_accepted`` / ``shots_degraded`` — terminal outcomes;
* ``shots_settled`` / ``percent_complete`` — completion against the plan;
* ``video_seconds`` — total video-seconds spent (budget feed, §11.1);
* ``last_event_at`` — recency for the "still generating" indicator.

**Replay-safety.** Per-book counts can only be derived from per-shot state, not
incremented blindly, or an at-least-once redelivery would double-count. The row
therefore carries a compact ``shots`` map (``shot_id -> {"terminal", "seconds"}``);
counts are *recomputed* from that map on every change. Combined with the runner's
per-``event_id`` dedupe this is doubly safe: the same event applied twice is both
skipped by the runner and idempotent in the fold.

Every handler threads the ``namespace`` argument the runner passes (never
``self.namespace``), so a rebuild slot / consistency-check scratch namespace is
honoured.

Consumed event types (domain discriminators, decoded from the envelope):
``ShotPlanned``, ``ShotRendered``, ``ShotAccepted``, ``ShotDegraded``,
``ShotRegenRequested``.
"""

from __future__ import annotations

from typing import Any

from app.datalayer.envelope import ProjectionEvent
from app.datalayer.projector import Projection, handles
from app.datalayer.readmodel import ReadModelStore

#: Terminal §9.7 outcomes a shot can settle into (mutually exclusive).
_TERMINAL_ACCEPTED = "accepted"
_TERMINAL_DEGRADED = "degraded"


class RenderProgressProjection(Projection):
    """One row per ``book_id`` summarising its shots' render progress."""

    name = "render_progress"
    version = 1

    async def _row(self, store: ReadModelStore, namespace: str, book_id: str) -> dict[str, Any]:
        existing = await store.get(namespace, book_id)
        if existing is not None:
            return existing.value
        return {
            "book_id": book_id,
            "shots_planned": 0,
            "shots_accepted": 0,
            "shots_degraded": 0,
            "shots_settled": 0,
            "percent_complete": 0.0,
            "video_seconds": 0.0,
            "last_event_at": None,
            # shot_id -> {"terminal": "accepted"|"degraded"|None, "seconds": float}
            "shots": {},
        }

    async def _recompute_and_put(
        self,
        store: ReadModelStore,
        namespace: str,
        book_id: str,
        row: dict[str, Any],
        event: ProjectionEvent,
    ) -> None:
        shots: dict[str, Any] = row["shots"]
        planned = len(shots)
        accepted = sum(1 for s in shots.values() if s.get("terminal") == _TERMINAL_ACCEPTED)
        degraded = sum(1 for s in shots.values() if s.get("terminal") == _TERMINAL_DEGRADED)
        settled = accepted + degraded
        video_seconds = round(sum(float(s.get("seconds", 0.0)) for s in shots.values()), 3)
        row["shots_planned"] = planned
        row["shots_accepted"] = accepted
        row["shots_degraded"] = degraded
        row["shots_settled"] = settled
        row["percent_complete"] = round(100.0 * settled / planned, 2) if planned else 0.0
        row["video_seconds"] = video_seconds
        row["last_event_at"] = _ts(event)
        await store.put(namespace, book_id, row)

    @staticmethod
    def _book_id(event: ProjectionEvent) -> str | None:
        bid = event.data.get("book_id")
        return bid if isinstance(bid, str) and bid else None

    @staticmethod
    def _shot_id(event: ProjectionEvent) -> str | None:
        sid = event.data.get("shot_id")
        return sid if isinstance(sid, str) and sid else None

    @handles("ShotPlanned")
    async def _on_planned(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        book_id = self._book_id(event)
        shot_id = self._shot_id(event)
        if book_id is None or shot_id is None:
            return
        row = await self._row(store, namespace, book_id)
        shots: dict[str, Any] = row["shots"]
        # Set-style: a shot's presence is idempotent; don't clobber a terminal one.
        shots.setdefault(shot_id, {"terminal": None, "seconds": 0.0})
        await self._recompute_and_put(store, namespace, book_id, row, event)

    @handles("ShotRendered")
    async def _on_rendered(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        # ShotRendered carries no book_id, but ShotPlanned always precedes it in
        # the same stream, so the shot already sits in exactly one book row. Set
        # the seconds absolutely (latest wins) => replay-safe.
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        seconds = _as_float(event.data.get("video_seconds"))
        row, book_id = await self._find_book_row_for_shot(store, namespace, shot_id)
        if row is None or book_id is None:
            return
        row["shots"][shot_id]["seconds"] = seconds
        await self._recompute_and_put(store, namespace, book_id, row, event)

    @handles("ShotAccepted")
    async def _on_accepted(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        await self._settle(store, namespace, event, _TERMINAL_ACCEPTED)

    @handles("ShotDegraded")
    async def _on_degraded(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        await self._settle(store, namespace, event, _TERMINAL_DEGRADED)

    @handles("ShotRegenRequested")
    async def _on_regen(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        # A regen re-opens a settled shot: clear its terminal so it counts as
        # in-flight again (counts recompute from the map => replay-safe).
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row, book_id = await self._find_book_row_for_shot(store, namespace, shot_id)
        if row is None or book_id is None:
            return
        row["shots"][shot_id]["terminal"] = None
        await self._recompute_and_put(store, namespace, book_id, row, event)

    async def _settle(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent, terminal: str
    ) -> None:
        shot_id = self._shot_id(event)
        if shot_id is None:
            return
        row, book_id = await self._find_book_row_for_shot(store, namespace, shot_id)
        if row is None or book_id is None:
            return
        row["shots"][shot_id]["terminal"] = terminal
        await self._recompute_and_put(store, namespace, book_id, row, event)

    async def _find_book_row_for_shot(
        self, store: ReadModelStore, namespace: str, shot_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Locate the book row that already lists ``shot_id`` (from its ShotPlanned).

        ShotPlanned precedes every other shot event in the stream and is the only
        event carrying ``book_id``, so by the time we see a render/accept/degrade
        the shot is already in exactly one book row.
        """
        for r in await store.list(namespace):
            if shot_id in r.value.get("shots", {}):
                return r.value, r.value["book_id"]
        return None, None


class RenderProgressRepository:
    """Read facade over the ``render_progress`` namespace."""

    def __init__(self, store: ReadModelStore, *, namespace: str = "render_progress") -> None:
        self._store = store
        self._namespace = namespace

    async def for_book(self, book_id: str) -> dict[str, Any] | None:
        """The progress row for ``book_id`` (without the internal ``shots`` map)."""
        row = await self._store.get(self._namespace, book_id)
        if row is None:
            return None
        return _public(row.value)

    async def all_books(self) -> list[dict[str, Any]]:
        """Every book's progress row, ordered by ``book_id``."""
        return [_public(r.value) for r in await self._store.list(self._namespace)]

    async def percent_complete(self, book_id: str) -> float:
        row = await self._store.get(self._namespace, book_id)
        return float(row.value.get("percent_complete", 0.0)) if row is not None else 0.0


def _public(value: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal per-shot bookkeeping from a row before returning it."""
    return {k: v for k, v in value.items() if k != "shots"}


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


__all__ = ["RenderProgressProjection", "RenderProgressRepository"]
