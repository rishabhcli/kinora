"""Shared response DTOs for the realtime + paginated surface (kinora.md §5.6).

These are the *transport* contracts for the new realtime routes, kept separate
from round-1's :mod:`app.api.schemas` so the additive surface never edits the
shared schema module. They project Redis-soft-state (presence, connection
counts, event-log resume info) and the pagination envelope into stable JSON.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.realtime.presence import RECOMMENDED_HEARTBEAT_S


class ParticipantView(BaseModel):
    """One reader in a shared session's presence roster (§5.2)."""

    participant_id: str
    user_id: str
    display: str
    focus_word: int
    mode: str
    joined_at_ms: int
    updated_at_ms: int


class PresenceRosterResponse(BaseModel):
    """The current roster for a session + the recommended heartbeat cadence."""

    session_id: str
    count: int
    participants: list[ParticipantView] = Field(default_factory=list)
    heartbeat_s: int = RECOMMENDED_HEARTBEAT_S


class JoinPresenceRequest(BaseModel):
    """Announce this client as a participant in a shared session (§5.2)."""

    model_config = ConfigDict(extra="forbid")

    display: str = Field(default="reader", min_length=1, max_length=80)
    focus_word: int = Field(default=0, ge=0)
    mode: str = Field(default="viewer")


class JoinPresenceResponse(BaseModel):
    """The freshly-joined participant + the rest of the roster (§5.2)."""

    participant_id: str
    session_id: str
    roster: list[ParticipantView] = Field(default_factory=list)
    heartbeat_s: int = RECOMMENDED_HEARTBEAT_S


class MovePresenceRequest(BaseModel):
    """Update this participant's cursor / mode (a presence ``move``)."""

    model_config = ConfigDict(extra="forbid")

    focus_word: int | None = Field(default=None, ge=0)
    mode: str | None = None


class PresenceAckResponse(BaseModel):
    """Acknowledgement of a heartbeat / move / leave (idempotent)."""

    session_id: str
    participant_id: str
    status: str  # "ok" | "expired" | "left"


class ConnectionStatsResponse(BaseModel):
    """Live connection bookkeeping for a session (presence headcount, §5.6)."""

    session_id: str
    live_connections: int
    presence_count: int
    max_per_session: int


class StreamInfoResponse(BaseModel):
    """Diagnostics for a session's resumable stream (event-log watermarks)."""

    session_id: str
    latest_event_id: int
    oldest_event_id: int
    retained: int


class EventEnvelope(BaseModel):
    """A single logged §5.6 event as a paginated history item."""

    id: int
    payload: dict[str, Any]


__all__ = [
    "ConnectionStatsResponse",
    "EventEnvelope",
    "JoinPresenceRequest",
    "JoinPresenceResponse",
    "MovePresenceRequest",
    "ParticipantView",
    "PresenceAckResponse",
    "PresenceRosterResponse",
    "StreamInfoResponse",
]
