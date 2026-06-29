"""Time seam for the defense subsystem.

Every detector and store in this package reads "now" through a :class:`Clock`
rather than calling :func:`time.monotonic`/:func:`time.time` directly, so the
whole engine is **deterministic under test**: a synthetic attack trace replays
through a :class:`ManualClock` and produces byte-identical scores every run.

Two clock notions are kept distinct on purpose:

* **wall** — UNIX epoch seconds (``float``), the timestamp carried on a security
  event and persisted on an alert. Monotonicity is *not* guaranteed (NTP steps).
* **mono** — a monotonic clock (``float`` seconds), used for windowed rate
  counters and decay so a backwards wall-clock step can never corrupt a window.

A real deployment injects :class:`SystemClock`; tests inject :class:`ManualClock`
and advance it explicitly. No module here imports a clock at module scope — the
engine receives one by constructor injection.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of the current time, injected into every time-aware component."""

    def wall(self) -> float:
        """UNIX epoch seconds. May jump (NTP); never use for window math."""

    def mono(self) -> float:
        """Monotonic seconds. Never decreases; use for windows and decay."""


class SystemClock:
    """Production clock backed by the real OS clocks."""

    __slots__ = ()

    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()


class ManualClock:
    """A fully controllable clock for deterministic tests.

    ``wall`` and ``mono`` advance together by default (``advance``) so the common
    case stays simple, but they can be moved independently to simulate NTP steps
    (``step_wall``) without disturbing window math.
    """

    __slots__ = ("_wall", "_mono")

    def __init__(self, *, wall: float = 1_700_000_000.0, mono: float = 0.0) -> None:
        self._wall = float(wall)
        self._mono = float(mono)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> ManualClock:
        """Advance both clocks by ``seconds`` (the usual test operation)."""
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._wall += seconds
        self._mono += seconds
        return self

    def step_wall(self, seconds: float) -> ManualClock:
        """Move *only* the wall clock (an NTP step). ``mono`` is untouched."""
        self._wall += seconds
        return self

    def set_wall(self, wall: float) -> ManualClock:
        self._wall = float(wall)
        return self

    def at(self, ts: float) -> ManualClock:
        """Set *both* clocks to absolute time ``ts``.

        The natural operation when replaying a recorded trace whose events carry
        their own timestamps: ``clock.at(event.ts)`` keeps the monotonic clock
        aligned with event time so windowed detectors see the right spacing.
        """
        self._wall = float(ts)
        self._mono = float(ts)
        return self
