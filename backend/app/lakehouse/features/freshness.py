"""Feature freshness — is the served value new enough to trust?

Freshness is the serving-time twin of the offline TTL. For a feature view with a
TTL, a value materialised at event time ``t`` and read at wall-clock ``now`` is:

* **fresh** while ``now - t <= ttl`` (still inside its validity window),
* **stale** once it crosses the TTL (it would be excluded by the point-in-time
  join, and the Redis online key is set to expire at the same boundary), and
* **expired/missing** when no value is present at all.

A freshness SLA can be *tighter* than the TTL (warn before a value is technically
stale). :func:`assess_freshness` classifies one value; :func:`freshness_report`
rolls a batch up into an SLA fraction the monitor surfaces. Pure functions — the
clock is always passed in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .types import FeatureView


class FreshnessState(StrEnum):
    FRESH = "fresh"
    WARN = "warn"  # within TTL but past the (tighter) SLA
    STALE = "stale"  # past the TTL
    MISSING = "missing"  # no value at all


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    view: str
    state: FreshnessState
    age: timedelta | None
    ttl: timedelta | None
    sla: timedelta | None


def assess_freshness(
    view: FeatureView,
    *,
    event_timestamp: datetime | None,
    now: datetime,
    sla: timedelta | None = None,
) -> FreshnessAssessment:
    """Classify a single online value's freshness against TTL + an optional SLA."""
    if event_timestamp is None:
        return FreshnessAssessment(view.name, FreshnessState.MISSING, None, view.ttl, sla)
    age = now - event_timestamp
    if age < timedelta(0):
        age = timedelta(0)  # a value "from the future" is treated as brand-new
    if view.ttl is not None and age >= view.ttl:
        state = FreshnessState.STALE
    elif sla is not None and age >= sla:
        state = FreshnessState.WARN
    else:
        state = FreshnessState.FRESH
    return FreshnessAssessment(view.name, state, age, view.ttl, sla)


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    view: str
    total: int
    fresh: int
    warn: int
    stale: int
    missing: int

    @property
    def sla_met_fraction(self) -> float:
        """Fraction of values that are FRESH (the headline SLA number)."""
        return 1.0 if self.total == 0 else self.fresh / self.total

    @property
    def ok(self) -> bool:
        return self.stale == 0 and self.missing == 0


def freshness_report(
    view: FeatureView,
    *,
    event_timestamps: Sequence[datetime | None],
    now: datetime,
    sla: timedelta | None = None,
) -> FreshnessReport:
    """Roll a batch of online values' event times up into an SLA report."""
    counts: dict[FreshnessState, int] = dict.fromkeys(FreshnessState, 0)
    for ts in event_timestamps:
        counts[assess_freshness(view, event_timestamp=ts, now=now, sla=sla).state] += 1
    return FreshnessReport(
        view=view.name,
        total=len(event_timestamps),
        fresh=counts[FreshnessState.FRESH],
        warn=counts[FreshnessState.WARN],
        stale=counts[FreshnessState.STALE],
        missing=counts[FreshnessState.MISSING],
    )


__all__ = [
    "FreshnessAssessment",
    "FreshnessReport",
    "FreshnessState",
    "assess_freshness",
    "freshness_report",
]
