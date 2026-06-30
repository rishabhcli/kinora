"""An injectable UTC clock for the privacy subsystem.

Retention is *deadline-driven* (a data class expires ``created_at + TTL``) and an
erasure certificate is timestamped, so every time-aware unit takes a
:class:`Clock`. Production uses :data:`system_clock`; tests use :class:`FixedClock`
to freeze and advance time deterministically.

This is a local copy of the same primitive :mod:`app.compliance.clock` exposes —
duplicated deliberately so :mod:`app.privacy` carries no import dependency on the
governance package (they are siblings that may land independently).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

#: A zero-argument callable returning the current timezone-aware UTC time.
Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(UTC)


class FixedClock:
    """A controllable clock for deterministic deadline tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, *, days: float = 0, hours: float = 0, seconds: float = 0) -> datetime:
        """Move the clock forward and return the new time."""
        self._now = self._now + timedelta(days=days, hours=hours, seconds=seconds)
        return self._now

    def set(self, when: datetime) -> None:
        """Pin the clock to ``when`` (must be timezone-aware)."""
        if when.tzinfo is None:  # pragma: no cover - guard against naive datetimes
            raise ValueError("FixedClock.set requires a timezone-aware datetime")
        self._now = when


def ensure_utc(value: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC (assume naive == UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["Clock", "FixedClock", "ensure_utc", "system_clock"]
