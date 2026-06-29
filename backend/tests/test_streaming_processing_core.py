"""Core engine tests: records, state, time, operators, windows, runtime.

These exercise the Flink-shaped primitives in isolation through the deterministic
:class:`TestHarness` and small end-to-end :class:`StreamEnvironment` jobs. No
infra, no wall-clock — every test reads as an explicit event-time timeline.
"""

from __future__ import annotations

from typing import Any

from app.streaming.processing import (
    CountAggregate,
    MeanAggregate,
    SlidingEventTimeWindows,
    SumAggregate,
    TumblingEventTimeWindows,
)
from app.streaming.processing.aggregations import MaxAggregate, MinAggregate, percentile
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.operators import (
    FilterOperator,
    FlatMapOperator,
    MapOperator,
)
from app.streaming.processing.records import LatePolicy
from app.streaming.processing.state import (
    InMemoryCheckpointStorage,
    KeyedStateBackend,
    ValueStateDescriptor,
)
from app.streaming.processing.testkit import TestHarness
from app.streaming.processing.time_domain import (
    BoundedOutOfOrdernessGenerator,
    WatermarkStrategy,
    field_timestamp_assigner,
)
from app.streaming.processing.window_operator import WindowOperator, WindowResult
from app.streaming.processing.windows import (
    TimeWindow,
    merge_windows,
)


# --------------------------------------------------------------------------- #
# Stateless operators
# --------------------------------------------------------------------------- #
def test_map_operator_preserves_timestamp_and_key() -> None:
    h: TestHarness[int, int] = TestHarness(MapOperator(lambda x: x * 2))
    h.process_value(21, timestamp=1000, key="k")
    rec = h.output[0]
    assert rec.value == 42
    assert rec.timestamp == 1000
    assert rec.key == "k"


def test_filter_operator() -> None:
    h: TestHarness[int, int] = TestHarness(FilterOperator(lambda x: x % 2 == 0))
    for v in range(5):
        h.process_value(v, timestamp=v)
    assert h.output_values() == [0, 2, 4]


def test_flat_map_operator_inherits_timestamp() -> None:
    h: TestHarness[str, str] = TestHarness(FlatMapOperator(lambda s: list(s)))
    h.process_value("ab", timestamp=7)
    assert h.output_values() == ["a", "b"]
    assert all(r.timestamp == 7 for r in h.output)


# --------------------------------------------------------------------------- #
# State backend + checkpointing
# --------------------------------------------------------------------------- #
def test_value_state_is_key_scoped() -> None:
    backend = KeyedStateBackend("op")
    desc: ValueStateDescriptor[int] = ValueStateDescriptor("count", default=0)
    state = backend.get_value_state(desc)

    backend.set_current_key("a")
    state.update(5)
    backend.set_current_key("b")
    assert state.value() == 0
    state.update(9)
    backend.set_current_key("a")
    assert state.value() == 5


def test_snapshot_is_immutable_against_later_mutation() -> None:
    backend = KeyedStateBackend("op")
    desc: ValueStateDescriptor[int] = ValueStateDescriptor("v", default=0)
    state = backend.get_value_state(desc)
    backend.set_current_key("a")
    state.update(1)

    snap = backend.snapshot(checkpoint_id=1)
    state.update(99)  # mutate after snapshot
    assert snap.namespaces["v"]["a"] == 1  # snapshot unchanged


def test_checkpoint_restore_roundtrip() -> None:
    storage = InMemoryCheckpointStorage()
    backend = KeyedStateBackend("op")
    desc: ValueStateDescriptor[int] = ValueStateDescriptor("v", default=0)
    state = backend.get_value_state(desc)
    backend.set_current_key("a")
    state.update(42)

    storage.put(backend.snapshot(7))
    backend.clear_all()
    backend.set_current_key("a")
    assert state.value() == 0  # cleared

    restored = storage.latest("op")
    assert restored is not None
    backend.restore(restored)
    backend.set_current_key("a")
    assert state.value() == 42


# --------------------------------------------------------------------------- #
# Watermark generators
# --------------------------------------------------------------------------- #
def test_bounded_out_of_orderness_watermark() -> None:
    gen = BoundedOutOfOrdernessGenerator(out_of_orderness_ms=100)
    gen.on_event(1_000)
    assert gen.current_watermark() == 1_000 - 100 - 1
    gen.on_event(900)  # out of order, does not lower the watermark
    assert gen.current_watermark() == 899
    gen.on_event(2_000)
    assert gen.current_watermark() == 1_899


# --------------------------------------------------------------------------- #
# Windows: tumbling / sliding / session
# --------------------------------------------------------------------------- #
def test_tumbling_assignment() -> None:
    win = TumblingEventTimeWindows(size_ms=1_000)
    assert win.assign(0) == [TimeWindow(0, 1_000)]
    assert win.assign(1_500) == [TimeWindow(1_000, 2_000)]


def test_sliding_assignment_counts() -> None:
    win = SlidingEventTimeWindows(size_ms=1_000, slide_ms=500)
    windows = win.assign(750)
    # 750 belongs to [0,1000) and [500,1500)
    assert TimeWindow(0, 1_000) in windows
    assert TimeWindow(500, 1_500) in windows
    assert len(windows) == 2


def test_session_merge_windows() -> None:
    merged = merge_windows(
        [TimeWindow(0, 10), TimeWindow(8, 18), TimeWindow(40, 50)]
    )
    targets = [w for w, _ in merged]
    assert TimeWindow(0, 18) in targets
    assert TimeWindow(40, 50) in targets
    assert len(targets) == 2


# --------------------------------------------------------------------------- #
# Window operator — tumbling count with on-time firing
# --------------------------------------------------------------------------- #
def _wm_strategy() -> WatermarkStrategy[dict]:
    return WatermarkStrategy.for_bounded_out_of_orderness(
        field_timestamp_assigner(lambda v: int(v["ts"])), out_of_orderness_ms=0
    )


def test_tumbling_window_count_fires_on_watermark() -> None:
    op: WindowOperator = WindowOperator(
        assigner=TumblingEventTimeWindows(size_ms=1_000),
        aggregate=CountAggregate(),
    )
    h: TestHarness = TestHarness(op)
    # three events in [0,1000), keyed "s1"
    for ts in (100, 200, 900):
        h.process_value({"ts": ts}, timestamp=ts, key="s1")
    # no output before the window closes
    assert h.output_values() == []
    h.process_watermark(1_000)  # watermark passes window max (999)
    results = h.output_values()
    assert len(results) == 1
    res: WindowResult = results[0]
    assert res.window == TimeWindow(0, 1_000)
    assert res.result == 3
    assert res.key == "s1"


def test_window_allowed_lateness_re_fires_then_drops() -> None:
    op: WindowOperator = WindowOperator(
        assigner=TumblingEventTimeWindows(size_ms=1_000),
        aggregate=CountAggregate(),
        late_policy=LatePolicy(allowed_lateness_ms=500),
    )
    h: TestHarness = TestHarness(op)
    h.process_value({"ts": 100}, timestamp=100, key="s1")
    h.process_watermark(1_000)  # on-time fire, count == 1
    assert h.output_values()[0].result == 1

    # late record within allowed-lateness (window end 1000 + 500 grace)
    h.process_value({"ts": 200}, timestamp=200, key="s1")
    late_fire = h.output_values()
    assert late_fire[-1].result == 2  # window re-fired with the late record
    assert late_fire[-1].is_update is True

    # advance past grace; cleanup drops state
    h.process_watermark(1_500)
    # a record now far too late goes to the side output
    h.process_value({"ts": 50}, timestamp=50, key="s1")
    assert len(h.side_output("late-records")) == 1


# --------------------------------------------------------------------------- #
# End-to-end DataStream job
# --------------------------------------------------------------------------- #
def test_end_to_end_tumbling_mean_job() -> None:
    env = StreamEnvironment()
    events: list[dict[str, Any]] = [
        {"session": "s1", "ts": 100, "v": 4.0},
        {"session": "s1", "ts": 400, "v": 6.0},
        {"session": "s2", "ts": 200, "v": 2.0},
        {"session": "s1", "ts": 1_200, "v": 8.0},
    ]
    out_node = (
        env.from_source(events)
        .assign_timestamps_and_watermarks(
            WatermarkStrategy.for_bounded_out_of_orderness(
                field_timestamp_assigner(lambda v: int(v["ts"])), 0
            )
        )
        .key_by(lambda v: v["session"])
        .window(TumblingEventTimeWindows(size_ms=1_000))
        .aggregate(MeanAggregate(lambda v: float(v["v"])))
    )
    result = env.execute()
    out = result.typed_values(out_node.node_id, WindowResult)
    by_key = {(r.key, r.window.start): r.result for r in out}
    assert by_key[("s1", 0)] == 5.0  # mean(4, 6)
    assert by_key[("s2", 0)] == 2.0
    assert by_key[("s1", 1_000)] == 8.0


def test_min_max_sum_aggregates() -> None:
    min_agg: MinAggregate[float] = MinAggregate()
    max_agg: MaxAggregate[float] = MaxAggregate()
    assert min_agg.add(3.0, min_agg.add(5.0, None)) == 3.0
    assert max_agg.add(3.0, max_agg.add(5.0, None)) == 5.0
    agg: SumAggregate[float] = SumAggregate()
    acc = agg.create_accumulator()
    for v in (1.0, 2.0, 3.0):
        acc = agg.add(v, acc)
    assert agg.get_result(acc) == 6.0


def test_percentile() -> None:
    assert percentile([], 0.5) is None
    assert percentile([10.0], 0.9) == 10.0
    assert percentile([0.0, 10.0], 0.5) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_checkpoint_every_n_records_completes() -> None:
    from app.streaming.processing.runtime import JobExecutor

    env = StreamEnvironment()
    events: list[dict[str, int]] = [{"ts": t, "v": 1} for t in range(10)]
    node = (
        env.from_source(events)
        .assign_timestamps_and_watermarks(
            WatermarkStrategy.for_bounded_out_of_orderness(
                field_timestamp_assigner(lambda v: int(v["ts"])), 0
            )
        )
        .key_by(lambda v: "k")
        .window(TumblingEventTimeWindows(size_ms=100))
        .aggregate(CountAggregate())
    )
    executor = JobExecutor(
        graph=env.graph,
        source_records=env._source_records,
        watermark_strategies=env._watermarks,
        checkpoint_every=3,
    )
    result = executor.run()
    assert len(result.checkpoints) >= 1
    # the windowed counts still total 10 events
    total = sum(r.result for r in result.typed_values(node.node_id, WindowResult))
    assert total == 10
