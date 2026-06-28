"""Usage metering — record consumption events and aggregate them per period.

This is the commercial mirror of the §11 budget ledger
(``app/db/models/budget.py``). Where the budget ledger meters *provider* cost in
video-seconds with reserve/commit/release rows, the usage ledger meters *reader*
consumption — reading-minutes, render-seconds, books imported, director edits —
as **append-only, idempotent** events that aggregate to a billable quantity per
metered price.

The design choices that match the budget ledger:

* **Append-only.** A usage record is never mutated; corrections are negative
  records. History survives.
* **Idempotent.** Each event carries a stable ``idempotency_key`` (e.g.
  ``shot_<id>:render_seconds``). Re-reporting the same event is a no-op, exactly
  like the queue's ``shot_hash`` idempotency — so a retried request can never
  double-bill.
* **Windowed-sum aggregation.** Billable quantity is a sum/max/last over the
  records that fall inside the subscription's current period, scoped by meter.

This module holds the **pure** in-memory model + aggregation. The DB-backed
recorder lives in ``repositories.py``; both share these value objects.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.billing.catalog import aggregate_usage
from app.billing.enums import MeteredAggregation, UsageMeter


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One recorded unit of consumption.

    ``quantity`` is in the meter's natural unit (minutes for READING_MINUTES,
    seconds for RENDER_SECONDS, a count for the discrete meters). ``at`` is the
    UTC instant the consumption happened (used to bucket it into a period).
    ``idempotency_key`` makes recording exactly-once.
    """

    meter: UsageMeter
    quantity: float
    at: datetime
    subscription_id: str | None = None
    customer_id: str | None = None
    book_id: str | None = None
    session_id: str | None = None
    idempotency_key: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("usage event 'at' must be timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class UsageQuantity:
    """The aggregated billable quantity for one meter over a window."""

    meter: UsageMeter
    aggregation: MeteredAggregation
    quantity: float
    event_count: int


class UsageLedger:
    """An in-memory append-only usage ledger with idempotent recording.

    Mirrors the budget ledger's interface shape for the *pure* (no-DB) case: the
    composition root and tests can drive metering end-to-end without infra, and
    the DB recorder implements the same recording semantics.
    """

    def __init__(self) -> None:
        self._events: list[UsageEvent] = []
        self._seen_keys: set[str] = set()

    def record(self, event: UsageEvent) -> bool:
        """Append ``event``; return False (no-op) if its idempotency key repeats."""
        if event.idempotency_key is not None:
            if event.idempotency_key in self._seen_keys:
                return False
            self._seen_keys.add(event.idempotency_key)
        self._events.append(event)
        return True

    def events(self) -> tuple[UsageEvent, ...]:
        return tuple(self._events)

    def in_window(
        self,
        *,
        meter: UsageMeter | None = None,
        subscription_id: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> list[UsageEvent]:
        """Events matching the meter/subscription scope and ``[start, end)`` window."""
        out = []
        for ev in self._events:
            if meter is not None and ev.meter is not meter:
                continue
            if subscription_id is not None and ev.subscription_id != subscription_id:
                continue
            if period_start is not None and ev.at < period_start:
                continue
            if period_end is not None and ev.at >= period_end:
                continue
            out.append(ev)
        return out

    def aggregate(
        self,
        meter: UsageMeter,
        aggregation: MeteredAggregation,
        *,
        subscription_id: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> UsageQuantity:
        """Collapse the matching events to a single billable quantity."""
        events = self.in_window(
            meter=meter,
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
        )
        quantity = aggregate_usage([e.quantity for e in events], aggregation)
        return UsageQuantity(
            meter=meter,
            aggregation=aggregation,
            quantity=quantity,
            event_count=len(events),
        )


@dataclass
class UsageSummary:
    """Per-meter aggregated totals for a period (the metering panel payload)."""

    period_start: datetime | None
    period_end: datetime | None
    by_meter: dict[UsageMeter, UsageQuantity] = field(default_factory=dict)

    def quantity(self, meter: UsageMeter) -> float:
        q = self.by_meter.get(meter)
        return q.quantity if q is not None else 0.0


def summarize(
    events: Iterable[UsageEvent],
    *,
    aggregations: dict[UsageMeter, MeteredAggregation] | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> UsageSummary:
    """Summarize a flat list of events into per-meter aggregated quantities.

    ``aggregations`` overrides the default per-meter collapse (SUM); meters not
    listed default to SUM. Useful for projecting a raw event stream into the API
    response without a ledger instance.
    """
    aggregations = aggregations or {}
    buckets: dict[UsageMeter, list[float]] = defaultdict(list)
    for ev in events:
        if period_start is not None and ev.at < period_start:
            continue
        if period_end is not None and ev.at >= period_end:
            continue
        buckets[ev.meter].append(ev.quantity)

    summary = UsageSummary(period_start=period_start, period_end=period_end)
    for meter, values in buckets.items():
        agg = aggregations.get(meter, MeteredAggregation.SUM)
        summary.by_meter[meter] = UsageQuantity(
            meter=meter,
            aggregation=agg,
            quantity=aggregate_usage(values, agg),
            event_count=len(values),
        )
    return summary


def render_seconds_event(
    *,
    seconds: float,
    at: datetime,
    subscription_id: str | None = None,
    shot_id: str | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
) -> UsageEvent:
    """Build a RENDER_SECONDS usage event keyed on the shot (idempotent per shot).

    This is the bridge from the render pipeline: when a shot's clip is accepted,
    the *actual* video-seconds spent (the same figure the budget ledger commits)
    become a billable render-seconds usage event. Keying on ``shot_id`` matches
    the queue's ``shot_hash`` idempotency so a re-accepted shot never double-bills.
    """
    key = f"render_seconds:{shot_id}" if shot_id else None
    return UsageEvent(
        meter=UsageMeter.RENDER_SECONDS,
        quantity=float(seconds),
        at=at,
        subscription_id=subscription_id,
        book_id=book_id,
        session_id=session_id,
        idempotency_key=key,
        note=f"shot {shot_id}" if shot_id else None,
    )


def reading_minutes_event(
    *,
    minutes: float,
    at: datetime,
    subscription_id: str | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
) -> UsageEvent:
    """Build a READING_MINUTES usage event (consumption that costs no credits)."""
    return UsageEvent(
        meter=UsageMeter.READING_MINUTES,
        quantity=float(minutes),
        at=at,
        subscription_id=subscription_id,
        book_id=book_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "UsageEvent",
    "UsageLedger",
    "UsageQuantity",
    "UsageSummary",
    "reading_minutes_event",
    "render_seconds_event",
    "summarize",
]
