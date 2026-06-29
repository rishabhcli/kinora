"""Advanced operators: union, co-process, split side-outputs, reduce windows."""

from __future__ import annotations

from typing import Any

from app.streaming.processing.aggregations import CountAggregate
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.operators import Collector, CoProcessFunction, SplitOperator
from app.streaming.processing.records import StreamRecord
from app.streaming.processing.state import ValueStateDescriptor
from app.streaming.processing.testkit import TestHarness
from app.streaming.processing.time_domain import (
    WatermarkStrategy,
    field_timestamp_assigner,
)
from app.streaming.processing.window_operator import WindowResult
from app.streaming.processing.windows import TumblingEventTimeWindows


def _wm() -> WatermarkStrategy[dict[str, Any]]:
    return WatermarkStrategy.for_bounded_out_of_orderness(
        field_timestamp_assigner(lambda v: int(v["ts"])), 0
    )


# --------------------------------------------------------------------------- #
# union — event-time interleave of two same-typed streams
# --------------------------------------------------------------------------- #
def test_union_interleaves_by_event_time() -> None:
    env = StreamEnvironment()
    a = env.from_source(
        [{"ts": 100, "src": "a"}, {"ts": 300, "src": "a"}], name="a"
    ).assign_timestamps_and_watermarks(_wm())
    b = env.from_source(
        [{"ts": 200, "src": "b"}], name="b"
    ).assign_timestamps_and_watermarks(_wm())
    merged = a.union(b)
    result = env.execute()
    records = result.records(merged.node_id)
    timestamps = [r.timestamp for r in records]
    assert timestamps == sorted(timestamps)
    assert len(records) == 3


def test_union_then_window_counts_all() -> None:
    env = StreamEnvironment()
    a = env.from_source(
        [{"ts": 100, "k": "k"}], name="a"
    ).assign_timestamps_and_watermarks(_wm())
    b = env.from_source(
        [{"ts": 200, "k": "k"}, {"ts": 300, "k": "k"}], name="b"
    ).assign_timestamps_and_watermarks(_wm())
    counts = (
        a.union(b)
        .key_by(lambda v: v["k"])
        .window(TumblingEventTimeWindows(size_ms=1_000))
        .aggregate(CountAggregate())
    )
    result = env.execute()
    totals = [w.result for w in result.typed_values(counts.node_id, WindowResult)]
    assert sum(totals) == 3


# --------------------------------------------------------------------------- #
# co-process — two-input keyed function over shared state
# --------------------------------------------------------------------------- #
class _PairUp(CoProcessFunction[str]):
    """Emits ``"<left>+<right>"`` once both sides have arrived for a key."""

    def __init__(self) -> None:
        self._left: ValueStateDescriptor[str] = ValueStateDescriptor("left", default="")
        self._right: ValueStateDescriptor[str] = ValueStateDescriptor("right", default="")

    def process_left(self, value: Any, record: StreamRecord[Any], out: Collector[str]) -> None:
        self.ctx.state.get_value_state(self._left).update(str(value["tok"]))
        self._maybe_emit(record.key, record.timestamp, out)

    def process_right(self, value: Any, record: StreamRecord[Any], out: Collector[str]) -> None:
        self.ctx.state.get_value_state(self._right).update(str(value["tok"]))
        self._maybe_emit(record.key, record.timestamp, out)

    def _maybe_emit(self, key: object, ts: int, out: Collector[str]) -> None:
        left = self.ctx.state.get_value_state(self._left).value() or ""
        right = self.ctx.state.get_value_state(self._right).value() or ""
        if left and right:
            out.collect_value(f"{left}+{right}", timestamp=ts, key=key)


def test_co_process_pairs_two_inputs() -> None:
    env = StreamEnvironment()
    left = (
        env.from_source([{"ts": 100, "k": "x", "tok": "L"}], name="left")
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["k"])
    )
    right = (
        env.from_source([{"ts": 150, "k": "x", "tok": "R"}], name="right")
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["k"])
    )
    paired = left.connect(right, _PairUp)
    result = env.execute()
    assert "L+R" in result.values(paired.node_id)


# --------------------------------------------------------------------------- #
# split — side outputs
# --------------------------------------------------------------------------- #
def test_split_operator_routes_to_side_outputs() -> None:
    h: TestHarness[dict[str, Any], dict[str, Any]] = TestHarness(
        SplitOperator(tag_selector=lambda v: v["lane"])
    )
    h.process_value({"lane": "fast", "x": 1}, timestamp=1)
    h.process_value({"lane": "slow", "x": 2}, timestamp=2)
    assert len(h.side_output("fast")) == 1
    assert len(h.side_output("slow")) == 1
    # split also forwards everything to the primary output
    assert len(h.output) == 2


# --------------------------------------------------------------------------- #
# windowed reduce
# --------------------------------------------------------------------------- #
def test_windowed_reduce_picks_max() -> None:
    env = StreamEnvironment()
    events: list[dict[str, Any]] = [
        {"ts": 100, "k": "k", "v": 3},
        {"ts": 200, "k": "k", "v": 9},
        {"ts": 300, "k": "k", "v": 5},
    ]
    reduced = (
        env.from_source(events)
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["k"])
        .window(TumblingEventTimeWindows(size_ms=1_000))
        .reduce(lambda a, b: a if int(a["v"]) >= int(b["v"]) else b)
    )
    result = env.execute()
    windows = result.typed_values(reduced.node_id, WindowResult)
    assert windows[0].result["v"] == 9
