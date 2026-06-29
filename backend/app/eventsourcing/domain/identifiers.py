"""Strongly-typed stream identifiers for the write side.

A stream id is an opaque string to the :class:`~app.eventsourcing.store.EventStore`,
but the domain wants a *typed* notion of "which aggregate kind, which id" so the
command bus can route, build deterministic stream names, and keep
session/render-shot/canon streams from ever colliding.

The convention is ``"{category}-{id}"`` (e.g. ``"session-abc123"``). The
``category`` is the lowercase aggregate kind; the id is the aggregate's own
identity (a uuid hex, a shot id, an entity id). Parsing is total and lossless.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: The separator between category and id in a stream name. Chosen so it never
#: appears in a category (categories are lowercase ascii words).
_SEP = "-"


class StreamCategory(StrEnum):
    """The aggregate kinds that own event streams in the write side."""

    SESSION = "session"
    RENDER_SHOT = "rendershot"
    CANON = "canon"


@dataclass(frozen=True, slots=True, order=True)
class StreamId:
    """A typed event-stream identity: a :class:`StreamCategory` plus an aggregate id.

    Use :meth:`value` to get the opaque string the store persists, and
    :meth:`parse` to recover the typed form from a stored stream name.
    """

    category: StreamCategory
    aggregate_id: str

    def __post_init__(self) -> None:
        if not self.aggregate_id:
            raise ValueError("aggregate_id must be non-empty")
        if _SEP in self.category:
            # Defensive: categories are an enum, so this is unreachable, but keep
            # the invariant explicit for any future category.
            raise ValueError(f"category {self.category!r} must not contain {_SEP!r}")

    @property
    def value(self) -> str:
        """The opaque ``"{category}-{id}"`` string the event store keys on."""
        return f"{self.category.value}{_SEP}{self.aggregate_id}"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, raw: str) -> StreamId:
        """Recover a :class:`StreamId` from its persisted string form.

        Raises:
            ValueError: when ``raw`` is not a ``"{known-category}-{id}"`` string.
        """
        category, sep, aggregate_id = raw.partition(_SEP)
        if not sep or not aggregate_id:
            raise ValueError(f"not a valid stream id: {raw!r}")
        try:
            cat = StreamCategory(category)
        except ValueError as exc:  # pragma: no cover - re-raised with context
            raise ValueError(f"unknown stream category in {raw!r}: {category!r}") from exc
        return cls(category=cat, aggregate_id=aggregate_id)

    @classmethod
    def session(cls, session_id: str) -> StreamId:
        """Stream id for a reading-session aggregate."""
        return cls(StreamCategory.SESSION, session_id)

    @classmethod
    def render_shot(cls, shot_id: str) -> StreamId:
        """Stream id for a §9.7 render-shot aggregate."""
        return cls(StreamCategory.RENDER_SHOT, shot_id)

    @classmethod
    def canon(cls, entity_id: str) -> StreamId:
        """Stream id for a canon-entity edit aggregate."""
        return cls(StreamCategory.CANON, entity_id)


__all__ = ["StreamCategory", "StreamId"]
