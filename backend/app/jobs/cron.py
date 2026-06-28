"""A minimal, dependency-free 5-field cron expression engine.

Supports the standard ``minute hour day-of-month month day-of-week`` fields with:

* ``*`` — every value,
* ``a`` — a single value,
* ``a-b`` — an inclusive range,
* ``a-b/n`` / ``*/n`` — a stepped range,
* ``a,b,c`` — a comma list of any of the above,
* day-of-week ``0`` or ``7`` == Sunday.

Day-of-month and day-of-week combine with the usual Vixie-cron OR semantics:
when *both* are restricted (neither is ``*``), a date matches if *either* field
matches; otherwise both must match. All evaluation is in UTC against an injected
clock — :func:`next_after` returns the next firing instant strictly after a given
time, which is what the scheduler uses to compute a job's due time.

This is intentionally small (no seconds, no ``@hourly`` macros, no timezones); it
covers the maintenance cadences this framework needs and is fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Inclusive (min, max) bounds for each of the five fields.
_FIELD_BOUNDS: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 7),  # day of week (0 and 7 == Sunday)
)
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
#: Hard cap on the forward search so a pathological expression can't hang.
_MAX_SEARCH_MINUTES = 366 * 24 * 60 * 4  # ~4 years


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(spec: str, lo: int, hi: int, name: str) -> frozenset[int]:
    """Expand one field spec into the explicit set of matching integers."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty term in {name} field: {spec!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"bad step in {name} field: {part!r}") from exc
            if step <= 0:
                raise CronError(f"step must be positive in {name} field: {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, _, end_s = base.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise CronError(f"bad range in {name} field: {base!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise CronError(f"bad value in {name} field: {base!r}") from exc

        if start > end:
            raise CronError(f"descending range in {name} field: {base!r}")
        if start < lo or end > hi:
            raise CronError(f"{name} value out of range [{lo},{hi}]: {base!r}")
        values.update(range(start, end + 1, step))

    # Normalise Sunday: cron allows both 0 and 7.
    if name == "day-of-week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A parsed 5-field cron expression, evaluable against UTC datetimes."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    expression: str
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        """Parse a 5-field cron ``expression`` (raises :class:`CronError`)."""
        fields = expression.split()
        if len(fields) != 5:
            raise CronError(
                f"cron expression must have 5 fields, got {len(fields)}: {expression!r}"
            )
        sets = [
            _parse_field(field, lo, hi, name)
            for field, (lo, hi), name in zip(fields, _FIELD_BOUNDS, _FIELD_NAMES, strict=True)
        ]
        return cls(
            minutes=sets[0],
            hours=sets[1],
            days_of_month=sets[2],
            months=sets[3],
            days_of_week=sets[4],
            expression=expression,
            dom_restricted=fields[2].strip() != "*",
            dow_restricted=fields[4].strip() != "*",
        )

    def matches(self, when: datetime) -> bool:
        """Whether ``when`` (truncated to the minute) satisfies this schedule."""
        if when.minute not in self.minutes:
            return False
        if when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False
        return self._day_matches(when)

    def _day_matches(self, when: datetime) -> bool:
        # Python weekday(): Mon=0..Sun=6 -> cron dow: Sun=0..Sat=6.
        cron_dow = (when.weekday() + 1) % 7
        dom_ok = when.day in self.days_of_month
        dow_ok = cron_dow in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime) -> datetime:
        """The next firing instant strictly after ``after`` (minute resolution)."""
        # Start at the next whole minute strictly after ``after``.
        candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(_MAX_SEARCH_MINUTES):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise CronError(f"no firing time found within search window for {self.expression!r}")


def parse_cron(expression: str) -> CronSchedule:
    """Convenience wrapper for :meth:`CronSchedule.parse`."""
    return CronSchedule.parse(expression)


__all__ = ["CronError", "CronSchedule", "parse_cron"]
