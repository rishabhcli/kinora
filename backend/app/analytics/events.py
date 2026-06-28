"""The typed product-analytics event taxonomy and envelopes.

This is the *contract* every event producer speaks. A producer (the desktop
renderer, the API, the scheduler) emits a :class:`RawEvent`; the ingestion path
scrubs it (:mod:`app.analytics.scrub`) and turns it into a :class:`TrackedEvent`
— the canonical, PII-safe row the store persists and every analysis runs over.

Design rules:

* **Closed, versioned taxonomy.** Event names are an :class:`EventName`
  ``StrEnum``. An unknown name is rejected at ingest (a typo never silently
  becomes a new metric). New names are added here, behind a code review.
* **Stable timestamps.** Every event carries an explicit, timezone-aware
  ``occurred_at``; the server also records ``received_at``. Analysis uses
  ``occurred_at`` (client truth) but clamps absurd clock skew (:mod:`scrub`).
* **No PII in the model.** The envelope only carries an *opaque* ``anon_user_id``
  (already hashed by the time a :class:`TrackedEvent` exists) and a bounded,
  allow-listed ``props`` dict. Free identifiers (email, file names) never appear.
* **Idempotency key.** ``event_id`` is client-supplied and unique; the store
  dedupes on it so a retried batch is harmless.

This module is pure: no I/O, no DB, no settings. It is safe to import anywhere.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Event taxonomy
# --------------------------------------------------------------------------- #


class EventName(enum.StrEnum):
    """The closed set of product-analytics event names.

    Grouped by surface. The string values are the wire form (snake_case,
    ``surface.verb``) so an event name is self-describing in a query.
    """

    # -- lifecycle / acquisition -------------------------------------------- #
    APP_OPENED = "app.opened"
    USER_SIGNED_UP = "user.signed_up"
    USER_LOGGED_IN = "user.logged_in"
    USER_LOGGED_OUT = "user.logged_out"

    # -- library / shelf ---------------------------------------------------- #
    SHELF_VIEWED = "shelf.viewed"
    BOOK_SEARCHED = "book.searched"
    BOOK_ADDED = "book.added"  # upload / public-domain add
    BOOK_IMPORT_STARTED = "book.import_started"
    BOOK_IMPORT_COMPLETED = "book.import_completed"
    BOOK_IMPORT_FAILED = "book.import_failed"
    BOOK_OPENED = "book.opened"
    BOOK_CLOSED = "book.closed"

    # -- reading / generation-on-scroll ------------------------------------- #
    READING_STARTED = "reading.started"
    PAGE_VIEWED = "page.viewed"
    PAGE_TURNED = "page.turned"
    SCROLL_INTENT = "scroll.intent"  # debounced focus-word/velocity push
    SEEK = "reading.seek"
    READING_ENDED = "reading.ended"

    # -- video stage -------------------------------------------------------- #
    CLIP_PLAYED = "clip.played"
    CLIP_COMPLETED = "clip.completed"
    BUFFER_STALL = "buffer.stall"  # a visible stall (engagement-negative)
    KEYFRAME_BRIDGE_SHOWN = "keyframe.bridge_shown"  # degraded lane surfaced

    # -- director mode ------------------------------------------------------ #
    MODE_SWITCHED = "mode.switched"  # viewer<->director
    DIRECTOR_COMMENT = "director.comment"
    DIRECTOR_REGEN = "director.regen"
    CANON_EDITED = "canon.edited"

    # -- generic engagement ------------------------------------------------- #
    FEATURE_USED = "feature.used"
    ERROR_SHOWN = "error.shown"

    @classmethod
    def is_known(cls, value: str) -> bool:
        """True iff ``value`` is a member's value (cheap membership test)."""
        return value in _KNOWN_EVENT_VALUES


_KNOWN_EVENT_VALUES: frozenset[str] = frozenset(member.value for member in EventName)


class ReadMode(enum.StrEnum):
    """Which pane drives the workspace when an event fired (mirrors §5.2)."""

    VIEWER = "viewer"
    DIRECTOR = "director"


#: Event names that, when present, define a "reading touch" for sessionization
#: and engagement — i.e. evidence a human is actively reading (not background
#: lifecycle noise like ``app.opened``).
READING_EVENTS: frozenset[EventName] = frozenset(
    {
        EventName.READING_STARTED,
        EventName.PAGE_VIEWED,
        EventName.PAGE_TURNED,
        EventName.SCROLL_INTENT,
        EventName.SEEK,
        EventName.CLIP_PLAYED,
        EventName.CLIP_COMPLETED,
        EventName.READING_ENDED,
    }
)


# --------------------------------------------------------------------------- #
# Envelopes
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass through aware ones unchanged."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RawEvent(BaseModel):
    """An event exactly as a client/producer reports it, **pre-scrub**.

    The ingestion path validates the name, scrubs ``props`` + identifiers, and
    converts this to a :class:`TrackedEvent`. ``user_ref`` here is whatever the
    caller has (an opaque id, or — never persisted raw — something that must be
    hashed); the scrubber turns it into ``anon_user_id``.
    """

    model_config = {"extra": "ignore"}

    event_id: str = Field(min_length=1, max_length=128)
    name: str
    occurred_at: datetime
    user_ref: str | None = None
    book_id: str | None = None
    session_ref: str | None = None
    mode: ReadMode | None = None
    props: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)

    @field_validator("name")
    @classmethod
    def _known_name(cls, value: str) -> str:
        if not EventName.is_known(value):
            raise ValueError(f"unknown analytics event name: {value!r}")
        return value

    @property
    def event_name(self) -> EventName:
        """The parsed :class:`EventName` (validated to be known)."""
        return EventName(self.name)


class EventBatch(BaseModel):
    """A batch of raw events from one ingest request (batched ingestion)."""

    events: list[RawEvent] = Field(default_factory=list)


class TrackedEvent(BaseModel):
    """A canonical, PII-safe, stored analytics event — the analysis unit.

    Differs from :class:`RawEvent` in that ``anon_user_id`` is already an opaque
    hash, ``props`` are already scrubbed/bounded, ``received_at`` is stamped, and
    every field has been validated. This is what the store holds and what every
    funnel/retention/engagement computation reads.
    """

    event_id: str = Field(min_length=1, max_length=128)
    name: EventName
    occurred_at: datetime
    received_at: datetime = Field(default_factory=_utcnow)
    anon_user_id: str | None = None
    book_id: str | None = None
    session_key: str | None = None
    mode: ReadMode | None = None
    props: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)

    def prop_float(self, key: str, default: float | None = None) -> float | None:
        """Read a numeric prop as float, tolerating ints/str; ``default`` on miss."""
        value = self.props.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def prop_int(self, key: str, default: int | None = None) -> int | None:
        """Read a numeric prop as int, tolerating floats/str; ``default`` on miss."""
        value = self.prop_float(key, None)
        if value is None:
            return default
        return int(value)

    def prop_str(self, key: str, default: str | None = None) -> str | None:
        """Read a prop as str; ``default`` when absent/None."""
        value = self.props.get(key)
        return default if value is None else str(value)


__all__ = [
    "READING_EVENTS",
    "EventBatch",
    "EventName",
    "RawEvent",
    "ReadMode",
    "TrackedEvent",
]
