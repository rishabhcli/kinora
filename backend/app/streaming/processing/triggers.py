"""Window triggers — *when* a window emits a result.

The assigner decides *which* window a record joins; the trigger decides *when*
that window fires. The default for event-time windows is
:class:`EventTimeTrigger`: fire once when the watermark passes the window's end,
then again on each late record that arrives within allowed-lateness.

:class:`CountTrigger` fires every ``n`` records (early results), and
:class:`PurgingTrigger` wraps any trigger to also clear window state on fire —
the combinators are enough to express the early/late firing policies Kinora's
dashboards want without a full Flink trigger DSL.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from app.streaming.processing.windows import TimeWindow


class TriggerResult(enum.Flag):
    """What a trigger decides on an event or timer.

    Combines a *fire* bit (emit the window result now) and a *purge* bit (clear
    the window state). ``FIRE_AND_PURGE`` is the common event-time terminal.
    """

    CONTINUE = 0
    FIRE = enum.auto()
    PURGE = enum.auto()
    FIRE_AND_PURGE = FIRE | PURGE

    @property
    def should_fire(self) -> bool:
        return bool(self & TriggerResult.FIRE)

    @property
    def should_purge(self) -> bool:
        return bool(self & TriggerResult.PURGE)


@dataclass(slots=True)
class TriggerContext:
    """State a trigger sees: the window, the current watermark, and a tiny
    per-window scratch dict it owns (e.g. a running count for a count trigger).
    """

    window: TimeWindow
    current_watermark: int
    scratch: dict[str, object]


class Trigger(Protocol):
    """Decides when a window fires.

    ``on_element`` runs per record; ``on_event_time`` runs when a registered
    event-time timer fires (the watermark crossed the window end). A trigger may
    register the window-end timer via ``register_end_timer`` returning the
    deadline the operator should arm.
    """

    def on_element(self, timestamp: int, ctx: TriggerContext) -> TriggerResult: ...

    def on_event_time(self, time: int, ctx: TriggerContext) -> TriggerResult: ...

    def end_timer(self, window: TimeWindow) -> int: ...


class EventTimeTrigger:
    """Fires when the watermark passes the window's max timestamp.

    The canonical event-time trigger: one on-time firing at ``window.end``, plus
    a firing per late record that still falls inside allowed-lateness (the
    operator arms a fresh timer for late updates). Purges when the cleanup timer
    (end + allowed-lateness) fires — handled by the operator, not here.
    """

    def on_element(self, timestamp: int, ctx: TriggerContext) -> TriggerResult:
        if ctx.window.max_timestamp <= ctx.current_watermark:
            # late element on an already-fired window -> fire again (update)
            return TriggerResult.FIRE
        return TriggerResult.CONTINUE

    def on_event_time(self, time: int, ctx: TriggerContext) -> TriggerResult:
        if time == ctx.window.max_timestamp:
            return TriggerResult.FIRE
        return TriggerResult.CONTINUE

    def end_timer(self, window: TimeWindow) -> int:
        return window.max_timestamp


@dataclass(slots=True)
class CountTrigger:
    """Fires every ``count`` elements regardless of event time (early results)."""

    count: int

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("count trigger threshold must be > 0")

    def on_element(self, timestamp: int, ctx: TriggerContext) -> TriggerResult:
        prev = ctx.scratch.get("count", 0)
        n = (prev if isinstance(prev, int) else 0) + 1
        if n >= self.count:
            ctx.scratch["count"] = 0
            return TriggerResult.FIRE
        ctx.scratch["count"] = n
        return TriggerResult.CONTINUE

    def on_event_time(self, time: int, ctx: TriggerContext) -> TriggerResult:
        return TriggerResult.CONTINUE

    def end_timer(self, window: TimeWindow) -> int:
        return window.max_timestamp


@dataclass(slots=True)
class PurgingTrigger:
    """Wraps a trigger so every FIRE also PURGEs the window state."""

    inner: Trigger

    def on_element(self, timestamp: int, ctx: TriggerContext) -> TriggerResult:
        result = self.inner.on_element(timestamp, ctx)
        return TriggerResult.FIRE_AND_PURGE if result.should_fire else result

    def on_event_time(self, time: int, ctx: TriggerContext) -> TriggerResult:
        result = self.inner.on_event_time(time, ctx)
        return TriggerResult.FIRE_AND_PURGE if result.should_fire else result

    def end_timer(self, window: TimeWindow) -> int:
        return self.inner.end_timer(window)
