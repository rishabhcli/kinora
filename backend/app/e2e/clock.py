"""A deterministic virtual clock for the end-to-end harness.

Reading "time" in a Kinora session is driven by the reader's word velocity, not
the wall clock. To keep a scenario deterministic we never call ``time.time`` —
instead the harness advances :class:`VirtualClock` explicitly (a page turn, a
dwell, a buffer fill) so every run produces the same trace. The clock exposes
both monotonic seconds (for ETA / buffer math, §4.3) and integer milliseconds
(the unit the scheduler uses for debounce / dwell / idle bookkeeping, §4.9).
"""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("app.e2e.clock")


class VirtualClock:
    """A monotonic clock the harness drives by hand (no wall-clock reads).

    Starts at ``t = 0`` and only ever moves forward. ``advance`` returns the new
    time so a scenario can chain reads, and never accepts a negative delta (time
    in a reading session is monotonic — a backward page turn still consumes
    real seconds).
    """

    def __init__(self, *, start_s: float = 0.0) -> None:
        if start_s < 0:
            raise ValueError("VirtualClock cannot start before zero")
        self._t = float(start_s)

    def now(self) -> float:
        """Current virtual time in seconds (monotonic)."""
        return self._t

    def now_ms(self) -> int:
        """Current virtual time in integer milliseconds (the scheduler's unit)."""
        return int(round(self._t * 1000.0))

    def advance(self, seconds: float) -> float:
        """Advance the clock by ``seconds`` (must be ``>= 0``); return the new time."""
        if seconds < 0:
            raise ValueError(f"cannot advance the virtual clock backward: {seconds}")
        self._t += float(seconds)
        return self._t

    def advance_ms(self, milliseconds: int) -> int:
        """Advance by whole milliseconds; return the new ``now_ms``."""
        if milliseconds < 0:
            raise ValueError(f"cannot advance the virtual clock backward: {milliseconds}")
        self._t += milliseconds / 1000.0
        return self.now_ms()


__all__ = ["VirtualClock"]
