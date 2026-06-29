"""The windowed-aggregation operator — event time, lateness, session merge.

This is the engine's centrepiece. For a keyed stream it:

1. **Assigns** each record to its windows (tumbling / sliding / session).
2. For **session** (merging) assigners, merges the record's window with any
   overlapping existing windows, folding their accumulators together.
3. **Aggregates** incrementally into per-(key, window) accumulator state — never
   buffering raw records (unless a :class:`CollectAggregate` is used).
4. **Triggers** per the configured trigger; the default fires when the watermark
   passes the window end.
5. Handles **allowed-lateness**: a window's state is kept until
   ``window.end + allowed_lateness``; a late record inside that grace period
   updates the window and re-fires it; a record later than that goes to the
   :data:`LATE_RECORDS_TAG` side output (and is counted), never silently lost.
6. **Cleans up** window state via an event-time cleanup timer.

The output of a window firing is a :class:`WindowResult` carrying the key, the
window bounds, and the aggregate result — the shape the dashboards consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from app.streaming.processing.aggregations import AggregateFunction
from app.streaming.processing.operators import BaseOperator, RuntimeContext
from app.streaming.processing.records import (
    LATE_RECORDS_TAG,
    LatePolicy,
    StreamRecord,
    Watermark,
)
from app.streaming.processing.state import MapStateDescriptor
from app.streaming.processing.triggers import (
    EventTimeTrigger,
    Trigger,
    TriggerContext,
)
from app.streaming.processing.windows import TimeWindow, WindowAssigner, merge_windows

IN = TypeVar("IN")
ACC = TypeVar("ACC")
OUT = TypeVar("OUT")


@dataclass(frozen=True, slots=True)
class WindowResult(Generic[OUT]):
    """One window firing: its key, bounds, result, and whether it was late.

    ``is_update`` is true when the firing is a *late re-fire* of an
    already-emitted window (within allowed-lateness), so a consumer can treat it
    as a correction rather than a fresh window.
    """

    key: object
    window: TimeWindow
    result: OUT
    is_update: bool = False


# Namespace key for the cleanup timer the operator arms per window.
def _cleanup_namespace(window: TimeWindow) -> str:
    return f"w:{window.start}:{window.end}"


def _parse_namespace(namespace: str) -> TimeWindow:
    _, start, end = namespace.split(":")
    return TimeWindow(int(start), int(end))


class WindowOperator(BaseOperator[IN, WindowResult[OUT]], Generic[IN, ACC, OUT]):
    """Keyed event-time window operator with allowed-lateness.

    Parameters
    ----------
    assigner:
        Maps a timestamp to its windows; ``is_merging`` switches on session merge.
    aggregate:
        The incremental aggregate folded per (key, window).
    trigger:
        When a window fires; defaults to :class:`EventTimeTrigger`.
    late_policy:
        Allowed-lateness and whether dropped-late records hit the side output.
    """

    def __init__(
        self,
        *,
        assigner: WindowAssigner,
        aggregate: AggregateFunction[IN, ACC, OUT],
        trigger: Trigger | None = None,
        late_policy: LatePolicy | None = None,
    ) -> None:
        self._assigner = assigner
        self._agg = aggregate
        self._trigger: Trigger = trigger or EventTimeTrigger()
        self._late = late_policy or LatePolicy()
        # per-key window accumulators:  MapState[window -> acc]
        self._acc_desc: MapStateDescriptor[TimeWindow, ACC] = MapStateDescriptor("win-acc")
        # per-key set of windows already fired on time (so a late firing is an update)
        self._fired_desc: MapStateDescriptor[TimeWindow, bool] = MapStateDescriptor("win-fired")
        # per-(key,window) trigger scratch
        self._trigger_scratch: dict[tuple[object, TimeWindow], dict[str, object]] = {}

    @property
    def is_keyed(self) -> bool:
        return True

    def open(self, ctx: RuntimeContext) -> None:
        super().open(ctx)

    # -- element path ------------------------------------------------------- #
    def process_record(self, record: StreamRecord[IN]) -> None:
        ctx = self.ctx
        ctx.state.set_current_key(record.key)
        ts = record.timestamp
        watermark = ctx.current_watermark

        acc_state = ctx.state.get_map_state(self._acc_desc)
        fired_state = ctx.state.get_map_state(self._fired_desc)

        for window in self._assigner.assign(ts):
            # Drop records too late even for the earliest window they'd join.
            if window.max_timestamp + self._late.allowed_lateness_ms < watermark:
                self._emit_late(record)
                continue

            if self._assigner.is_merging:
                self._add_with_merge(record.key, window, record.value, acc_state, fired_state)
            else:
                self._add_simple(record.key, window, record.value, acc_state)
                self._arm_cleanup(record.key, window)

            self._maybe_trigger_on_element(record, window, acc_state, fired_state)

    def _add_simple(
        self,
        key: object,
        window: TimeWindow,
        value: IN,
        acc_state: object,
    ) -> None:
        acc = acc_state.get(window)  # type: ignore[attr-defined]
        if acc is None:
            acc = self._agg.create_accumulator()
        acc_state.put(window, self._agg.add(value, acc))  # type: ignore[attr-defined]

    def _add_with_merge(
        self,
        key: object,
        new_window: TimeWindow,
        value: IN,
        acc_state: object,
        fired_state: object,
    ) -> None:
        # Build the full window set including the new one, then coalesce.
        existing = list(acc_state.items())  # type: ignore[attr-defined]
        all_windows = [w for w, _ in existing] + [new_window]
        merged = merge_windows(all_windows)

        # Find the merged window the new record falls into.
        for target, members in merged:
            if not (target.start <= new_window.start and new_window.end <= target.end):
                continue
            combined: ACC | None = None
            for member in members:
                if member == new_window:
                    continue
                macc = acc_state.get(member)  # type: ignore[attr-defined]
                if macc is None:
                    continue
                combined = macc if combined is None else self._agg.merge(combined, macc)
                acc_state.remove(member)  # type: ignore[attr-defined]
                fired_state.remove(member)  # type: ignore[attr-defined]
                self.ctx.timers.delete(key, _cleanup_namespace(member), member.max_timestamp)
                self._trigger_scratch.pop((key, member), None)
            if combined is None:
                combined = self._agg.create_accumulator()
            acc_state.put(target, self._agg.add(value, combined))  # type: ignore[attr-defined]
            self._arm_cleanup(key, target)
            return

    def _arm_cleanup(self, key: object, window: TimeWindow) -> None:
        deadline = window.max_timestamp + self._late.allowed_lateness_ms
        self.ctx.timers.register(key, _cleanup_namespace(window), deadline)
        # also arm the on-time firing at window end
        self.ctx.timers.register(key, _cleanup_namespace(window), window.max_timestamp)

    def _maybe_trigger_on_element(
        self,
        record: StreamRecord[IN],
        window: TimeWindow,
        acc_state: object,
        fired_state: object,
    ) -> None:
        tctx = TriggerContext(
            window=window,
            current_watermark=self.ctx.current_watermark,
            scratch=self._trigger_scratch.setdefault((record.key, window), {}),
        )
        result = self._trigger.on_element(record.timestamp, tctx)
        if result.should_fire:
            self._fire(record.key, window, acc_state, fired_state, purge=result.should_purge)

    def _emit_late(self, record: StreamRecord[IN]) -> None:
        if self._late.emit_late_to_side_output:
            self.out.collect_side(LATE_RECORDS_TAG, record)  # type: ignore[arg-type]

    # -- watermark / timer path -------------------------------------------- #
    def process_watermark(self, watermark: Watermark) -> None:
        ctx = self.ctx
        for key, namespace, timestamp in ctx.timers.advance_watermark(watermark.timestamp):
            window = _parse_namespace(namespace)
            ctx.state.set_current_key(key)
            acc_state = ctx.state.get_map_state(self._acc_desc)
            fired_state = ctx.state.get_map_state(self._fired_desc)

            if timestamp == window.max_timestamp:
                # on-time firing
                tctx = TriggerContext(
                    window=window,
                    current_watermark=watermark.timestamp,
                    scratch=self._trigger_scratch.setdefault((key, window), {}),
                )
                tr = self._trigger.on_event_time(timestamp, tctx)
                if tr.should_fire:
                    self._fire(key, window, acc_state, fired_state, purge=tr.should_purge)
            elif timestamp == window.max_timestamp + self._late.allowed_lateness_ms:
                # cleanup: window has outlived allowed-lateness, drop its state
                acc_state.remove(window)
                fired_state.remove(window)
                self._trigger_scratch.pop((key, window), None)

    def _fire(
        self,
        key: object,
        window: TimeWindow,
        acc_state: object,
        fired_state: object,
        *,
        purge: bool,
    ) -> None:
        acc = acc_state.get(window)  # type: ignore[attr-defined]
        if acc is None:
            return
        is_update = bool(fired_state.get(window, False))  # type: ignore[attr-defined]
        result = self._agg.get_result(acc)
        self.out.collect_value(
            WindowResult(key=key, window=window, result=result, is_update=is_update),
            timestamp=window.max_timestamp,
            key=key,
        )
        fired_state.put(window, True)  # type: ignore[attr-defined]
        if purge:
            acc_state.remove(window)  # type: ignore[attr-defined]
