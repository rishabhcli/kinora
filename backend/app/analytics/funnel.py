"""Ordered-step funnel analysis with a conversion window.

A *funnel* is an ordered sequence of event names (steps). For each user we walk
their event stream and advance through the steps **in order**: a user is counted
at step *k* if they fired step *k* after reaching step *k-1*, within an optional
``window`` of the first step. The result reports per-step user counts, the
overall and step-to-step conversion ratios, the drop-off at each step, and the
median time-to-convert from first step to last.

This is the standard "first-touch ordered funnel" definition: each step must
occur strictly after the previous step's matched event (ties broken by
``event_id`` for determinism). Pure; deterministic given the events.

Example — the acquisition funnel for Kinora:
``[app.opened, book.added, book.import_completed, book.opened, reading.started]``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from app.analytics.events import EventName, TrackedEvent


@dataclass(frozen=True)
class FunnelStep:
    """One step's outcome in a funnel."""

    name: EventName
    users: int
    conversion_from_start: float  # users / step0 users (0..1)
    conversion_from_prev: float  # users / previous step users (0..1)
    dropoff_from_prev: int  # previous - this


@dataclass(frozen=True)
class FunnelResult:
    """The full funnel: per-step counts + the overall conversion + timing."""

    steps: list[FunnelStep]
    total_entered: int  # users who fired step 0
    total_converted: int  # users who reached the last step
    overall_conversion: float
    median_time_to_convert_s: float | None

    def step(self, name: EventName) -> FunnelStep | None:
        """Return the step entry for ``name`` (first match), or ``None``."""
        for s in self.steps:
            if s.name == name:
                return s
        return None


def _user_streams(events: list[TrackedEvent]) -> dict[str, list[TrackedEvent]]:
    """Group events by ``anon_user_id`` (skipping anonymous), sorted in time."""
    by_user: dict[str, list[TrackedEvent]] = defaultdict(list)
    for event in events:
        if event.anon_user_id is None:
            continue
        by_user[event.anon_user_id].append(event)
    for stream in by_user.values():
        stream.sort(key=lambda e: (e.occurred_at, e.event_id))
    return by_user


def _walk_user(
    stream: list[TrackedEvent],
    steps: list[EventName],
    window: timedelta | None,
) -> tuple[int, datetime | None, datetime | None]:
    """Return (deepest step index reached +? , first-step time, matched-last time).

    Returns the count of steps the user reached (0..len(steps)), the timestamp of
    the matched step 0, and the timestamp of the deepest matched step.
    """
    reached = 0
    start_time: datetime | None = None
    last_time: datetime | None = None
    deadline: datetime | None = None
    cursor_after: datetime | None = None  # matched events must come after this
    for target in steps:
        matched: TrackedEvent | None = None
        for event in stream:
            if event.name != target:
                continue
            if cursor_after is not None and event.occurred_at < cursor_after:
                continue
            if deadline is not None and event.occurred_at > deadline:
                continue
            matched = event
            break
        if matched is None:
            break
        reached += 1
        last_time = matched.occurred_at
        if start_time is None:
            start_time = matched.occurred_at
            if window is not None:
                deadline = start_time + window
        cursor_after = matched.occurred_at
    return reached, start_time, last_time


def analyze_funnel(
    events: list[TrackedEvent],
    steps: list[EventName],
    *,
    window: timedelta | None = None,
) -> FunnelResult:
    """Compute the ordered funnel over ``steps`` for the users in ``events``.

    Args:
        events: the scrubbed event population (any users / books).
        steps: the ordered step event names (length >= 1).
        window: optional max elapsed time from step 0 for the rest to count.
    """
    if not steps:
        raise ValueError("a funnel needs at least one step")

    streams = _user_streams(events)
    # depth_counts[k] = number of users who reached AT LEAST step k (1-indexed
    # depth: depth 1 == fired step 0).
    depth_counts = [0] * (len(steps) + 1)
    convert_times: list[float] = []

    for stream in streams.values():
        reached, start_time, last_time = _walk_user(stream, steps, window)
        for d in range(reached + 1):
            depth_counts[d] += 1
        if reached == len(steps) and start_time is not None and last_time is not None:
            convert_times.append((last_time - start_time).total_seconds())

    entered = depth_counts[1] if len(steps) >= 1 else 0
    step_results: list[FunnelStep] = []
    prev_users = entered
    for i, name in enumerate(steps):
        users = depth_counts[i + 1]
        conv_start = (users / entered) if entered else 0.0
        conv_prev = (users / prev_users) if prev_users else 0.0
        dropoff = max(0, prev_users - users)
        step_results.append(
            FunnelStep(
                name=name,
                users=users,
                conversion_from_start=conv_start,
                conversion_from_prev=conv_prev if i > 0 else 1.0,
                dropoff_from_prev=dropoff if i > 0 else 0,
            )
        )
        prev_users = users

    converted = depth_counts[len(steps)]
    overall = (converted / entered) if entered else 0.0
    return FunnelResult(
        steps=step_results,
        total_entered=entered,
        total_converted=converted,
        overall_conversion=overall,
        median_time_to_convert_s=median(convert_times) if convert_times else None,
    )


__all__ = ["FunnelResult", "FunnelStep", "analyze_funnel"]
