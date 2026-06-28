"""Quiet-hours math — pure, timezone-aware, no I/O.

A reader can declare a daily *quiet window* (e.g. 22:00–07:00 local) during which
non-urgent notifications are *held* rather than sent, and released at the next
window boundary. This module is the pure arithmetic behind that gate:

* :func:`is_quiet` — is ``now`` inside the window, honouring windows that wrap
  past midnight (start > end)?
* :func:`next_open_at` — the next instant the window is *not* quiet, so the
  dispatcher knows when to schedule a held notification.

Urgent notifications (``NotificationPriority.URGENT`` — e.g. a surfaced conflict
blocking generation) bypass quiet hours; that policy lives in the dispatcher, not
here. This module only answers "is it quiet?" and "when does it open?".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_tz(name: str | None) -> tzinfo:
    """Resolve an IANA tz name to a ``tzinfo`` (falls back to UTC on unknown)."""
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A daily quiet window in a named timezone.

    ``start``/``end`` are local wall-clock times. A window where ``start <= end``
    is same-day (09:00–17:00 = quiet during the day); ``start > end`` wraps past
    midnight (22:00–07:00 = quiet overnight). ``start == end`` means *no* quiet
    window (zero-width), so it never suppresses anything.
    """

    start: time
    end: time
    tz_name: str = "UTC"
    enabled: bool = True

    @property
    def tz(self) -> tzinfo:
        return resolve_tz(self.tz_name)

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end

    def is_quiet(self, now: datetime) -> bool:
        """Whether ``now`` falls inside the quiet window."""
        if not self.enabled or self.start == self.end:
            return False
        local = _as_local(now, self.tz).timetz().replace(tzinfo=None)
        if self.wraps_midnight:
            # e.g. 22:00–07:00: quiet if at/after start OR before end.
            return local >= self.start or local < self.end
        # e.g. 09:00–17:00: quiet if within [start, end).
        return self.start <= local < self.end

    def next_open_at(self, now: datetime) -> datetime:
        """The next UTC instant at which the window is no longer quiet.

        If not currently quiet, returns ``now`` unchanged (already open).
        """
        if not self.is_quiet(now):
            return now
        local_now = _as_local(now, self.tz)
        today_end = local_now.replace(
            hour=self.end.hour,
            minute=self.end.minute,
            second=self.end.second,
            microsecond=0,
        )
        # The window's end is the open boundary. For a wrapping window the end may
        # be "tomorrow" relative to a late-night ``now``; for a same-day window the
        # end is always later the same day (we are inside [start, end)).
        open_local = today_end if today_end > local_now else today_end + timedelta(days=1)
        return open_local.astimezone(UTC)


def is_quiet(window: QuietHours | None, now: datetime) -> bool:
    """Convenience: ``False`` for a missing window, else ``window.is_quiet(now)``."""
    return bool(window and window.is_quiet(now))


def next_open_at(window: QuietHours | None, now: datetime) -> datetime:
    """Convenience: ``now`` for a missing/open window, else the next open instant."""
    if window is None:
        return now
    return window.next_open_at(now)


def _as_local(now: datetime, tz: tzinfo) -> datetime:
    """Coerce ``now`` to a tz-aware datetime in ``tz`` (naive is treated as UTC)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(tz)


__all__ = ["QuietHours", "is_quiet", "next_open_at", "resolve_tz"]
