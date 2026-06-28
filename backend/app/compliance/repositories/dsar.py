"""Repository for DSAR requests and their transition events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.compliance.db.models import DSAREvent, DSARRequest
from app.compliance.enums import DSARKind, DSARState
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class DSARRepo(BaseRepository):
    """Create, transition and query DSARs + their append-only event log."""

    async def create(
        self,
        *,
        subject_id: str,
        kind: DSARKind,
        received_at: datetime,
        due_at: datetime,
        subject_email: str | None = None,
        note: str | None = None,
        request_id: str | None = None,
    ) -> DSARRequest:
        """Insert a new DSAR in the RECEIVED state."""
        request = DSARRequest(
            id=request_id or new_id(),
            subject_id=subject_id,
            subject_email=subject_email,
            kind=kind,
            state=DSARState.RECEIVED,
            received_at=received_at,
            due_at=due_at,
            note=note,
        )
        self.session.add(request)
        await self.session.flush()
        return request

    async def get(self, request_id: str) -> DSARRequest | None:
        """Fetch a DSAR by id."""
        return await self.session.get(DSARRequest, request_id)

    async def list_for_subject(self, subject_id: str) -> list[DSARRequest]:
        """Every DSAR a subject has filed, newest first."""
        stmt = (
            select(DSARRequest)
            .where(DSARRequest.subject_id == subject_id)
            .order_by(DSARRequest.received_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_open(self) -> list[DSARRequest]:
        """Every DSAR not yet in a terminal state, oldest deadline first."""
        terminal = (DSARState.COMPLETED, DSARState.REJECTED, DSARState.CANCELLED)
        stmt = (
            select(DSARRequest)
            .where(DSARRequest.state.not_in(terminal))
            .order_by(DSARRequest.due_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_overdue(self, now: datetime) -> list[DSARRequest]:
        """Open DSARs whose effective deadline has passed (SLA breach watch)."""
        return [r for r in await self.list_open() if (r.extended_due_at or r.due_at) < now]

    async def save_state(
        self,
        request: DSARRequest,
        *,
        state: DSARState,
        completed_at: datetime | None = None,
        extended_due_at: datetime | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Persist a state transition on the request row."""
        request.state = state
        if completed_at is not None:
            request.completed_at = completed_at
        if extended_due_at is not None:
            request.extended_due_at = extended_due_at
        if result is not None:
            request.result = result
        await self.session.flush()

    async def append_event(
        self,
        *,
        request_id: str,
        from_state: DSARState | None,
        to_state: DSARState,
        actor_id: str = "system",
        detail: dict[str, Any] | None = None,
    ) -> DSAREvent:
        """Append one transition event to the DSAR's audit log."""
        event = DSAREvent(
            id=new_id(),
            request_id=request_id,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            detail=detail,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def events(self, request_id: str) -> list[DSAREvent]:
        """The transition history for a DSAR, oldest first."""
        stmt = (
            select(DSAREvent)
            .where(DSAREvent.request_id == request_id)
            .order_by(DSAREvent.seq.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["DSARRepo"]
