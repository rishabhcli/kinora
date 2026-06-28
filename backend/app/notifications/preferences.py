"""Per-user notification preferences (opt-in matrix + quiet hours + digest).

A reader controls *what* they're notified about, *where* (which channels), *when*
(quiet hours), and *how often* (immediate vs digest). This module is the value
type + the pure gate that the dispatcher consults:

* :class:`NotificationPreferences` — the matrix of (event → enabled channels), a
  global :class:`~app.notifications.quiet_hours.QuietHours` window, a digest
  cadence, and a master mute.
* :meth:`NotificationPreferences.channels_for` — the resolved channel set for an
  event (intersecting per-event opt-ins with globally enabled channels).
* sensible **defaults** so a brand-new user is reachable without configuring
  anything (in-app always on; email on for the high-signal events).

The quiet-hours / digest *timing* decisions live in the dispatcher (which has the
clock); this module only declares the policy.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, Field

from app.notifications.events import DomainEvent
from app.notifications.models import Channel, NotificationPriority
from app.notifications.quiet_hours import QuietHours


class DigestCadence(BaseModel):
    """How often digestable notifications are batched + flushed.

    ``enabled`` off means immediate delivery. ``interval_minutes`` is the rollup
    window; the dispatcher flushes a digest once an interval's worth of items has
    accumulated (or on an explicit flush).
    """

    enabled: bool = False
    interval_minutes: int = 60

    @property
    def interval_seconds(self) -> float:
        return max(self.interval_minutes, 1) * 60.0


#: The channels every user is opted into for an event unless they change it.
#: In-app is always on (cheap, durable inbox); email on for high-signal events.
_DEFAULT_MATRIX: dict[DomainEvent, frozenset[Channel]] = {
    DomainEvent.BOOK_READY: frozenset({Channel.IN_APP, Channel.EMAIL}),
    DomainEvent.BOOK_FAILED: frozenset({Channel.IN_APP, Channel.EMAIL}),
    DomainEvent.RENDER_DONE: frozenset({Channel.IN_APP}),
    DomainEvent.REGEN_DONE: frozenset({Channel.IN_APP}),
    DomainEvent.BUDGET_LOW: frozenset({Channel.IN_APP, Channel.EMAIL}),
    DomainEvent.CONFLICT_SURFACED: frozenset({Channel.IN_APP, Channel.PUSH}),
    DomainEvent.RENDER_DEADLETTER: frozenset({Channel.IN_APP}),
    DomainEvent.DIGEST_READY: frozenset({Channel.EMAIL, Channel.IN_APP}),
}


class NotificationPreferences(BaseModel):
    """A user's notification settings."""

    user_id: str
    #: Master switch — when False, everything except URGENT is suppressed.
    enabled: bool = True
    #: Channels enabled globally; an event's channels are intersected with this.
    enabled_channels: frozenset[Channel] = Field(
        default_factory=lambda: frozenset(
            {Channel.IN_APP, Channel.EMAIL, Channel.PUSH, Channel.WEBHOOK}
        )
    )
    #: Per-event opt-in matrix (event → channels). Missing events use the default.
    matrix: dict[DomainEvent, frozenset[Channel]] = Field(default_factory=dict)
    quiet_hours: QuietHours | None = None
    digest: DigestCadence = Field(default_factory=DigestCadence)
    locale: str = "en"

    @classmethod
    def defaults(cls, user_id: str, *, locale: str = "en") -> NotificationPreferences:
        """A fresh, sensible default preference set for ``user_id``."""
        return cls(user_id=user_id, matrix=dict(_DEFAULT_MATRIX), locale=locale)

    def channels_for(
        self, event: DomainEvent, *, priority: NotificationPriority = NotificationPriority.NORMAL
    ) -> frozenset[Channel]:
        """The channels a notification for ``event`` should go out on.

        Resolution: the per-event opt-ins (falling back to the built-in default
        for that event), intersected with the globally enabled channels. A master
        mute suppresses everything *except* URGENT, which is always reachable on
        in-app + push so a blocking decision is never lost.
        """
        if not self.enabled and not priority.bypasses_quiet_hours:
            return frozenset()
        wanted = self.matrix.get(event)
        if wanted is None:
            wanted = _DEFAULT_MATRIX.get(event, frozenset({Channel.IN_APP}))
        resolved = wanted & self.enabled_channels
        if priority.bypasses_quiet_hours and not self.enabled:
            # Master-muted but urgent: keep the always-reachable rails open.
            return frozenset({Channel.IN_APP, Channel.PUSH}) & (
                self.enabled_channels | {Channel.IN_APP}
            )
        return resolved

    def wants(self, event: DomainEvent, channel: Channel) -> bool:
        """Whether ``channel`` is enabled for ``event`` under these prefs."""
        return channel in self.channels_for(event)

    def with_event_channels(
        self, event: DomainEvent, channels: frozenset[Channel]
    ) -> NotificationPreferences:
        """Return a copy with ``event``'s channel opt-ins replaced (API edits)."""
        matrix = dict(self.matrix)
        matrix[event] = channels
        return self.model_copy(update={"matrix": matrix})


# A few well-known quiet windows for the API / defaults (constructed lazily).
def overnight_quiet(tz_name: str = "UTC") -> QuietHours:
    """A 22:00–07:00 overnight quiet window in ``tz_name``."""
    return QuietHours(start=time(22, 0), end=time(7, 0), tz_name=tz_name)


__all__ = [
    "DigestCadence",
    "NotificationPreferences",
    "overnight_quiet",
]
