"""Repository for reading sessions / scheduler state (kinora.md §4.9)."""

from __future__ import annotations

from typing import Any

from app.db.models.enums import SessionMode
from app.db.models.session import Session
from app.db.repositories.base import BaseRepository


class SessionRepo(BaseRepository):
    """Get, upsert, and patch the per-reader session row."""

    async def get(self, session_id: str) -> Session | None:
        """Fetch a session by id."""
        return await self.session.get(Session, session_id)

    async def upsert(
        self,
        *,
        session_id: str,
        book_id: str,
        user_id: str | None = None,
        focus_word: int = 0,
        velocity_wps: float = 4.0,
        committed_seconds_ahead: float = 0.0,
        mode: SessionMode = SessionMode.VIEWER,
        inflight: dict[str, Any] | None = None,
        budget_remaining_s: float | None = None,
        last_activity_ms: int | None = None,
    ) -> Session:
        """Create the session, or overwrite its mutable fields if it exists."""
        existing = await self.session.get(Session, session_id)
        if existing is None:
            existing = Session(
                id=session_id,
                book_id=book_id,
                user_id=user_id,
                focus_word=focus_word,
                velocity_wps=velocity_wps,
                committed_seconds_ahead=committed_seconds_ahead,
                mode=mode,
                inflight=inflight,
                budget_remaining_s=budget_remaining_s,
                last_activity_ms=last_activity_ms,
            )
            self.session.add(existing)
        else:
            existing.book_id = book_id
            existing.user_id = user_id
            existing.focus_word = focus_word
            existing.velocity_wps = velocity_wps
            existing.committed_seconds_ahead = committed_seconds_ahead
            existing.mode = mode
            existing.inflight = inflight
            existing.budget_remaining_s = budget_remaining_s
            existing.last_activity_ms = last_activity_ms
        await self.session.flush()
        return existing

    async def update_fields(self, session_id: str, **fields: Any) -> Session | None:
        """Patch arbitrary session columns (focus word, velocity, budget, ...)."""
        row = await self.session.get(Session, session_id)
        if row is None:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        await self.session.flush()
        return row
