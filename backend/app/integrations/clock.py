"""An injectable clock + sleeper so backoff/timing is deterministic in tests.

Anything in the integrations framework that reads "now" or sleeps does it
through a :class:`Clock`, never ``time``/``asyncio.sleep`` directly. Production
wires :class:`SystemClock`; tests wire :class:`FakeClock`, which advances time
manually and records every sleep — so a backoff schedule can be asserted on
without the test actually waiting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Time source + cooperative sleeper."""

    def now(self) -> datetime:
        """The current UTC time (timezone-aware)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` (cooperatively)."""
        ...


class SystemClock:
    """The real wall-clock + ``asyncio.sleep`` sleeper."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """A controllable clock: ``now`` advances only when you sleep or ``advance``.

    Every :meth:`sleep` is recorded in :attr:`slept` and advances the internal
    clock — so a backoff loop completes instantly and its delays are inspectable.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward without recording a sleep."""
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(max(0.0, seconds))


__all__ = ["Clock", "FakeClock", "SystemClock"]
