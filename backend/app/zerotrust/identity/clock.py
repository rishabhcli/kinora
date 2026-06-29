"""Injectable clock for deterministic identity/KMS time arithmetic.

Every TTL, lease window, certificate ``notBefore``/``notAfter``, key-rotation
schedule, and JWT ``exp`` in this package is computed against a :class:`Clock`
seam rather than :func:`time.time` directly. Production wires
:class:`SystemClock`; tests wire :class:`FixedClock` / :class:`ManualClock` so
expiry, rotation, and renewal logic is exercised without ``sleep`` and the crypto
fixtures stay byte-for-byte reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def _aware(moment: datetime) -> datetime:
    """Coerce *moment* to a timezone-aware UTC datetime."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class Clock(Protocol):
    """A source of the current wall-clock instant (UTC, timezone-aware)."""

    def now(self) -> datetime:  # pragma: no cover - protocol
        ...


class SystemClock:
    """The real clock: :func:`datetime.now` in UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """A clock frozen at a fixed instant — the default for crypto tests."""

    __slots__ = ("_at",)

    def __init__(self, at: datetime) -> None:
        self._at = _aware(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        """Jump the frozen instant to *at* (UTC-coerced)."""

        self._at = _aware(at)


class ManualClock:
    """A clock that only advances when :meth:`advance` is called.

    Lets a test drive a whole rotation/lease lifecycle deterministically:
    issue at T0, ``advance(hours=1)``, assert the credential is now expiring.
    """

    __slots__ = ("_at",)

    def __init__(self, at: datetime) -> None:
        self._at = _aware(at)

    def now(self) -> datetime:
        return self._at

    def advance(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
    ) -> datetime:
        """Advance the clock by the given delta and return the new instant."""

        self._at = self._at + timedelta(
            seconds=seconds, minutes=minutes, hours=hours, days=days
        )
        return self._at


__all__ = ["Clock", "FixedClock", "ManualClock", "SystemClock"]
