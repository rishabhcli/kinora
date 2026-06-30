"""Per-session activity read model (folds the reading-Session stream, §5.2-§5.4).

The Session aggregate (:mod:`app.eventsourcing.domain.session`) emits a domain
event per reading-session fact: the reader opened a book, intent moved, mode
flipped Viewer<->Director, a Director comment routed to an agent, a preference was
written back (§9.6), and finally the session ended. This projection folds those,
keyed by ``session_id``, into one activity row the director bar reads:

* ``user_id`` / ``book_id`` — who/what, from the genesis ``SessionStarted``;
* ``mode`` — current Viewer/Director mode (absolute, last-write-wins);
* ``focus_word`` / ``velocity`` — latest reading intent (§4.3);
* ``comment_count`` / ``preference_count`` — directing activity (absolute counts
  recomputed from sets of ids, so redelivery cannot inflate them);
* ``status`` / ``ended_reason`` — ``active`` until ``SessionEnded``;
* ``started_at`` / ``last_event_at`` — lifecycle timestamps.

**Replay-safety.** Counts are derived from id sets (``comment_ids``,
``preference_keys``) recomputed on every change rather than incremented, so an
at-least-once redelivery is idempotent even before the runner's event-id dedupe.

Consumed event types: ``SessionStarted``, ``IntentUpdated``, ``ModeSwitched``,
``DirectorCommentLeft``, ``PreferenceRecorded``, ``SessionEnded``.
"""

from __future__ import annotations

from typing import Any

from app.datalayer.envelope import ProjectionEvent
from app.datalayer.projector import Projection, handles
from app.datalayer.readmodel import ReadModelStore

_STATUS_ACTIVE = "active"
_STATUS_ENDED = "ended"


class SessionActivityProjection(Projection):
    """One activity row per ``session_id``."""

    name = "session_activity"
    version = 1

    @staticmethod
    def _session_id(event: ProjectionEvent) -> str | None:
        sid = event.data.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
        # Fall back to the stream id ("<category>-<session_id>") suffix.
        if "-" in event.stream_id:
            return event.stream_id.split("-", 1)[1]
        return event.stream_id or None

    async def _row(
        self, store: ReadModelStore, namespace: str, session_id: str
    ) -> dict[str, Any]:
        existing = await store.get(namespace, session_id)
        if existing is not None:
            return existing.value
        return {
            "session_id": session_id,
            "user_id": None,
            "book_id": None,
            "mode": None,
            "focus_word": 0,
            "velocity": 0.0,
            "comment_count": 0,
            "preference_count": 0,
            "status": _STATUS_ACTIVE,
            "ended_reason": None,
            "started_at": None,
            "last_event_at": None,
            # bookkeeping sets (kept internal; stripped on read)
            "comment_ids": [],
            "preference_keys": [],
        }

    async def _put(
        self,
        store: ReadModelStore,
        namespace: str,
        session_id: str,
        row: dict[str, Any],
        event: ProjectionEvent,
    ) -> None:
        row["comment_count"] = len(row["comment_ids"])
        row["preference_count"] = len(row["preference_keys"])
        row["last_event_at"] = _ts(event)
        await store.put(namespace, session_id, row)

    @handles("SessionStarted")
    async def _on_started(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        row["user_id"] = event.data.get("user_id") or row["user_id"]
        row["book_id"] = event.data.get("book_id") or row["book_id"]
        row["mode"] = event.data.get("mode") or row["mode"]
        row["status"] = _STATUS_ACTIVE
        row["started_at"] = event.data.get("started_at") or _ts(event)
        await self._put(store, namespace, session_id, row, event)

    @handles("IntentUpdated")
    async def _on_intent(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        row["focus_word"] = _as_int(event.data.get("focus_word"))
        row["velocity"] = _as_float(event.data.get("velocity"))
        await self._put(store, namespace, session_id, row, event)

    @handles("ModeSwitched")
    async def _on_mode(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        row["mode"] = event.data.get("mode") or row["mode"]
        await self._put(store, namespace, session_id, row, event)

    @handles("DirectorCommentLeft")
    async def _on_comment(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        comment_id = event.data.get("comment_id") or event.event_id
        ids: list[str] = row["comment_ids"]
        if comment_id not in ids:
            ids.append(comment_id)
        await self._put(store, namespace, session_id, row, event)

    @handles("PreferenceRecorded")
    async def _on_preference(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        key = event.data.get("key")
        keys: list[str] = row["preference_keys"]
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
        await self._put(store, namespace, session_id, row, event)

    @handles("SessionEnded")
    async def _on_ended(
        self, store: ReadModelStore, namespace: str, event: ProjectionEvent
    ) -> None:
        session_id = self._session_id(event)
        if session_id is None:
            return
        row = await self._row(store, namespace, session_id)
        row["status"] = _STATUS_ENDED
        row["ended_reason"] = event.data.get("reason") or "closed"
        await self._put(store, namespace, session_id, row, event)


class SessionActivityRepository:
    """Read facade over the ``session_activity`` namespace."""

    def __init__(self, store: ReadModelStore, *, namespace: str = "session_activity") -> None:
        self._store = store
        self._namespace = namespace

    async def for_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self._store.get(self._namespace, session_id)
        return _public(row.value) if row is not None else None

    async def active_sessions(self) -> list[dict[str, Any]]:
        """Every session whose status is still ``active`` (ordered by id)."""
        return [
            _public(r.value)
            for r in await self._store.list(self._namespace)
            if r.value.get("status") == _STATUS_ACTIVE
        ]

    async def for_book(self, book_id: str) -> list[dict[str, Any]]:
        """Every session row for ``book_id`` (active or ended)."""
        return [
            _public(r.value)
            for r in await self._store.list(self._namespace)
            if r.value.get("book_id") == book_id
        ]


def _public(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k not in ("comment_ids", "preference_keys")}


def _ts(event: ProjectionEvent) -> str | None:
    return event.recorded_at.isoformat() if event.recorded_at is not None else None


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


__all__ = ["SessionActivityProjection", "SessionActivityRepository"]
