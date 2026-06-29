"""The reading-Session aggregate (kinora.md §5.2-§5.4, §9.6).

A reading session is the client/SyncEngine's lifecycle on the write side: a
reader opens a book, scrolls (intent updates), flips between Viewer and Director
mode, leaves Director comments that route to agents, and eventually the session
ends (idle-swept or closed) and its directing preferences are written back to
memory (§9.6 — "every director edit writes back into memory").

This aggregate models that lifecycle as an event stream. The decision methods are
pure: each validates against current state, then :meth:`~AggregateRoot.emit`s the
fact. Intent updates are intentionally *not* one-event-per-scroll — that would be
a firehose; instead the latest intent is folded into state and only re-emitted
when it changes materially, keeping the stream a meaningful audit trail rather
than telemetry (the high-frequency stream stays in the scheduler, not here).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.db.models.enums import SessionMode
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation, ValidationError
from app.eventsourcing.domain.events import DomainEvent, register_events
from app.eventsourcing.domain.identifiers import StreamCategory
from app.eventsourcing.domain.snapshotting import as_bool, as_float, as_int, as_str

# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionStarted(DomainEvent):
    """A reader opened a book and began a session (starts in Viewer mode)."""

    session_id: str = ""
    user_id: str = ""
    book_id: str = ""
    mode: str = SessionMode.VIEWER.value
    started_at: str | None = None


@dataclass(frozen=True, slots=True)
class IntentUpdated(DomainEvent):
    """The reader's attention moved materially (§4.3 — focus word + velocity)."""

    session_id: str = ""
    focus_word: int = 0
    velocity: float = 0.0


@dataclass(frozen=True, slots=True)
class ModeSwitched(DomainEvent):
    """The liquid-glass control flipped between Viewer and Director (§5.2)."""

    session_id: str = ""
    mode: str = SessionMode.VIEWER.value


@dataclass(frozen=True, slots=True)
class DirectorCommentLeft(DomainEvent):
    """A Director-mode region comment, routed to an agent (§5.4, §7).

    Per the design note, this is the REST regen path: the comment targets a shot
    and carries the routed agent, so a saga can trigger the shot's regeneration.
    """

    session_id: str = ""
    comment_id: str = ""
    shot_id: str = ""
    note: str = ""
    routed_agent: str = ""
    region: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class PreferenceRecorded(DomainEvent):
    """A directing preference inferred from this session (§9.6 write-back)."""

    session_id: str = ""
    user_id: str = ""
    key: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class SessionEnded(DomainEvent):
    """The session closed — explicitly or by the idle-sweeper (§6)."""

    session_id: str = ""
    reason: str = "closed"
    ended_at: str | None = None


register_events(
    SessionStarted,
    IntentUpdated,
    ModeSwitched,
    DirectorCommentLeft,
    PreferenceRecorded,
    SessionEnded,
)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #

_VELOCITY_EPSILON = 0.05
_FOCUS_EPSILON = 3


@dataclass(slots=True)
class SessionAggregate(AggregateRoot):
    """Event-sourced reading session.

    State is rebuilt from the stream; decision methods enforce the lifecycle:
    a session must be started before any other command, cannot be started twice,
    and accepts no commands once ended.
    """

    category = StreamCategory.SESSION

    user_id: str = ""
    book_id: str = ""
    mode: SessionMode = SessionMode.VIEWER
    started: bool = False
    ended: bool = False
    focus_word: int = 0
    velocity: float = 0.0
    comment_count: int = 0
    preferences: dict[str, str] = field(default_factory=dict)

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self.user_id = ""
        self.book_id = ""
        self.mode = SessionMode.VIEWER
        self.started = False
        self.ended = False
        self.focus_word = 0
        self.velocity = 0.0
        self.comment_count = 0
        self.preferences = {}

    # -- decisions ----------------------------------------------------------- #

    def start(
        self,
        *,
        user_id: str,
        book_id: str,
        started_at: datetime | None = None,
    ) -> None:
        """Open the session (Viewer mode). Idempotent only via the bus key."""
        if self.started:
            raise CommandRejected(f"session {self.aggregate_id} already started")
        if not user_id or not book_id:
            raise ValidationError("user_id and book_id are required")
        self.emit(
            SessionStarted(
                session_id=self.aggregate_id,
                user_id=user_id,
                book_id=book_id,
                mode=SessionMode.VIEWER.value,
                started_at=started_at.isoformat() if started_at else None,
            )
        )

    def update_intent(self, *, focus_word: int, velocity: float) -> bool:
        """Fold a scroll intent; emit only on a *material* change.

        Returns whether an event was emitted (so a caller can tell a no-op apart
        from a real move). A sub-epsilon nudge is absorbed into state silently to
        keep the stream from becoming a scroll firehose.
        """
        self._require_live()
        if focus_word < 0:
            raise ValidationError("focus_word must be non-negative", field="focus_word")
        material = (
            abs(focus_word - self.focus_word) >= _FOCUS_EPSILON
            or abs(velocity - self.velocity) >= _VELOCITY_EPSILON
        )
        if not material:
            return False
        self.emit(
            IntentUpdated(
                session_id=self.aggregate_id,
                focus_word=focus_word,
                velocity=velocity,
            )
        )
        return True

    def switch_mode(self, *, mode: SessionMode) -> bool:
        """Flip Viewer<->Director. A no-op (same mode) emits nothing."""
        self._require_live()
        if mode == self.mode:
            return False
        self.emit(ModeSwitched(session_id=self.aggregate_id, mode=mode.value))
        return True

    def leave_comment(
        self,
        *,
        comment_id: str,
        shot_id: str,
        note: str,
        routed_agent: str,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Leave a Director region comment routed to an agent (§5.4)."""
        self._require_live()
        if self.mode is not SessionMode.DIRECTOR:
            raise InvariantViolation("comments are only allowed in Director mode")
        if not note.strip():
            raise ValidationError("comment note must not be empty", field="note")
        if not shot_id:
            raise ValidationError("a comment must target a shot", field="shot_id")
        self.emit(
            DirectorCommentLeft(
                session_id=self.aggregate_id,
                comment_id=comment_id,
                shot_id=shot_id,
                note=note,
                routed_agent=routed_agent,
                region=region,
            )
        )

    def record_preference(self, *, key: str, value: str) -> bool:
        """Record an inferred directing preference (§9.6). No-op if unchanged."""
        self._require_live()
        if not key:
            raise ValidationError("preference key is required", field="key")
        if self.preferences.get(key) == value:
            return False
        self.emit(
            PreferenceRecorded(
                session_id=self.aggregate_id,
                user_id=self.user_id,
                key=key,
                value=value,
            )
        )
        return True

    def end(self, *, reason: str = "closed", ended_at: datetime | None = None) -> bool:
        """End the session. Idempotent: ending an ended session is a no-op."""
        if not self.started:
            raise CommandRejected("cannot end a session that never started")
        if self.ended:
            return False
        self.emit(
            SessionEnded(
                session_id=self.aggregate_id,
                reason=reason,
                ended_at=ended_at.isoformat() if ended_at else None,
            )
        )
        return True

    # -- guards -------------------------------------------------------------- #

    def _require_live(self) -> None:
        if not self.started:
            raise CommandRejected(f"session {self.aggregate_id} has not started")
        if self.ended:
            raise CommandRejected(f"session {self.aggregate_id} has ended")

    # -- fold ---------------------------------------------------------------- #

    def apply(self, event: DomainEvent) -> None:
        if isinstance(event, SessionStarted):
            self.started = True
            self.user_id = event.user_id
            self.book_id = event.book_id
            self.mode = SessionMode(event.mode)
        elif isinstance(event, IntentUpdated):
            self.focus_word = event.focus_word
            self.velocity = event.velocity
        elif isinstance(event, ModeSwitched):
            self.mode = SessionMode(event.mode)
        elif isinstance(event, DirectorCommentLeft):
            self.comment_count += 1
        elif isinstance(event, PreferenceRecorded):
            self.preferences[event.key] = event.value
        elif isinstance(event, SessionEnded):
            self.ended = True
        # Unknown events are ignored (forward compatibility).

    # -- snapshotting -------------------------------------------------------- #

    def snapshot_state(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "book_id": self.book_id,
            "mode": self.mode.value,
            "started": self.started,
            "ended": self.ended,
            "focus_word": self.focus_word,
            "velocity": self.velocity,
            "comment_count": self.comment_count,
            "preferences": dict(self.preferences),
        }

    def restore_state(self, state: Mapping[str, object], *, version: int) -> None:
        self.user_id = as_str(state.get("user_id"))
        self.book_id = as_str(state.get("book_id"))
        self.mode = SessionMode(as_str(state.get("mode"), SessionMode.VIEWER.value))
        self.started = as_bool(state.get("started"))
        self.ended = as_bool(state.get("ended"))
        self.focus_word = as_int(state.get("focus_word"))
        self.velocity = as_float(state.get("velocity"))
        self.comment_count = as_int(state.get("comment_count"))
        prefs = state.get("preferences", {})
        self.preferences = (
            {str(k): as_str(v) for k, v in prefs.items()} if isinstance(prefs, Mapping) else {}
        )
        self.version = version
        self._committed_version = version


__all__ = [
    "DirectorCommentLeft",
    "IntentUpdated",
    "ModeSwitched",
    "PreferenceRecorded",
    "SessionAggregate",
    "SessionEnded",
    "SessionStarted",
]
