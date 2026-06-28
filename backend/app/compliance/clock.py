"""An injectable UTC clock.

Compliance is *deadline-driven* — a DSAR has a one-month statutory clock, a
retention rule has a TTL, a policy has an effective window. Hard-coding
``datetime.now`` makes those untestable, so every time-aware service takes a
:class:`Clock`. Production uses :data:`system_clock`; tests use
:class:`FixedClock` to freeze and advance time deterministically.
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
    """A controllable clock for deterministic deadline tests.

    Starts at ``start`` (defaults to a fixed epoch) and only moves when
    :meth:`advance` / :meth:`set` is called, so a test can assert exactly what a
    deadline computation does without sleeping.
    """

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
