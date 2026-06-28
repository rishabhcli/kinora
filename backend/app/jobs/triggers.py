"""Triggers — the *when* of a scheduled job.

A trigger answers one question: given the last fire time (or ``None`` for a job
that has never fired) and the current instant, **what is the next instant this
job should fire?** Returning ``None`` means "never again" (an exhausted one-shot).

Four flavours:

* :class:`CronTrigger` — a 5-field cron expression (UTC).
* :class:`IntervalTrigger` — every ``N`` seconds, optionally anchored.
* :class:`OnceTrigger` — fire exactly once at/after a given instant.
* :class:`ManualTrigger` — never fires on a schedule (only ``run_now``).

All triggers are pure and clock-free: the scheduler passes in instants. Each
exposes a :attr:`kind` so runs/metrics can be tagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.jobs.cron import CronSchedule, parse_cron
from app.jobs.types import TriggerKind


@runtime_checkable
class Trigger(Protocol):
    """Compute the next fire instant for a job."""

    @property
    def kind(self) -> TriggerKind:
        """The trigger flavour (for tagging runs / metrics)."""
        ...

    def next_fire(self, *, after: datetime, last_fire: datetime | None) -> datetime | None:
        """Next fire instant strictly after ``after`` (``None`` => never again)."""
        ...


@dataclass(frozen=True, slots=True)
class CronTrigger:
    """Fire on a 5-field cron schedule (UTC)."""

    schedule: CronSchedule

    @classmethod
    def parse(cls, expression: str) -> CronTrigger:
        """Build from a cron ``expression`` string."""
        return cls(parse_cron(expression))

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.CRON

    def next_fire(self, *, after: datetime, last_fire: datetime | None) -> datetime | None:
        return self.schedule.next_after(after)

    def __repr__(self) -> str:
        return f"CronTrigger({self.schedule.expression!r})"


@dataclass(frozen=True, slots=True)
class IntervalTrigger:
    """Fire every ``seconds`` seconds.

    Without an ``anchor`` the first fire is ``after + seconds`` and each
    subsequent fire is ``last_fire + seconds`` (drift-free relative to fires).
    With an ``anchor`` the schedule is the grid ``anchor + k*seconds`` and the
    next fire is the smallest grid point strictly after ``after`` — a steady
    wall-clock cadence that survives restarts.
    """

    seconds: float
    anchor: datetime | None = None

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError("interval seconds must be positive")

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.INTERVAL

    def next_fire(self, *, after: datetime, last_fire: datetime | None) -> datetime | None:
        step = timedelta(seconds=self.seconds)
        if self.anchor is not None:
            if after < self.anchor:
                return self.anchor
            elapsed = (after - self.anchor).total_seconds()
            k = int(elapsed // self.seconds) + 1
            return self.anchor + k * step
        base = last_fire if last_fire is not None else after
        nxt = base + step
        # If we've fallen behind (loop was paused), catch up to just after ``after``.
        if nxt <= after:
            behind = (after - base).total_seconds()
            k = int(behind // self.seconds) + 1
            nxt = base + k * step
        return nxt


@dataclass(frozen=True, slots=True)
class OnceTrigger:
    """Fire exactly once, at or after ``at``; never again once it has fired."""

    at: datetime

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.ONCE

    def next_fire(self, *, after: datetime, last_fire: datetime | None) -> datetime | None:
        if last_fire is not None:
            return None  # already fired
        return self.at if self.at > after else after


@dataclass(frozen=True, slots=True)
class ManualTrigger:
    """Never fires on a schedule — the job runs only via an explicit ``run_now``."""

    @property
    def kind(self) -> TriggerKind:
        return TriggerKind.MANUAL

    def next_fire(self, *, after: datetime, last_fire: datetime | None) -> datetime | None:
        return None


def every(seconds: float, *, anchor: datetime | None = None) -> IntervalTrigger:
    """Convenience constructor: ``every(30)`` -> an :class:`IntervalTrigger`."""
    return IntervalTrigger(seconds=seconds, anchor=anchor)


def cron(expression: str) -> CronTrigger:
    """Convenience constructor: ``cron("0 3 * * *")`` -> a :class:`CronTrigger`."""
    return CronTrigger.parse(expression)


def once(at: datetime) -> OnceTrigger:
    """Convenience constructor for a one-shot trigger at ``at``."""
    return OnceTrigger(at=at)


def manual() -> ManualTrigger:
    """Convenience constructor for a manual (never-auto-fire) trigger."""
    return ManualTrigger()


__all__ = [
    "CronTrigger",
    "IntervalTrigger",
    "ManualTrigger",
    "OnceTrigger",
    "Trigger",
    "cron",
    "every",
    "manual",
    "once",
]
