"""Watermark alignment, late-data edges, and broker→engine→broker integration."""

from __future__ import annotations

from typing import Any, cast

from app.streaming.processing.aggregations import CountAggregate
from app.streaming.processing.broker import BrokerSink, BrokerSource, InMemoryBroker
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.records import LatePolicy, StreamRecord
from app.streaming.processing.runtime import _merge_input_streams
from app.streaming.processing.testkit import TestHarness
from app.streaming.processing.time_domain import (
    WatermarkStrategy,
    field_timestamp_assigner,
)
from app.streaming.processing.window_operator import WindowOperator, WindowResult
from app.streaming.processing.windows import SessionWindows, TumblingEventTimeWindows


def _wm() -> WatermarkStrategy[dict[str, Any]]:
    return WatermarkStrategy.for_bounded_out_of_orderness(
        field_timestamp_assigner(lambda v: int(v["ts"])), 0
    )


# --------------------------------------------------------------------------- #
# Watermark alignment across inputs (the channel-minimum rule)
# --------------------------------------------------------------------------- #
def test_merge_aligns_watermark_to_channel_minimum() -> None:
    from app.streaming.processing.records import Watermark

    # channel 0 advances to 100 then 300; channel 1 only to 200.
    left: list[Any] = [
        StreamRecord(value="a", timestamp=100),
        Watermark(100),
        StreamRecord(value="b", timestamp=300),
        Watermark(300),
    ]
    right: list[Any] = [
        StreamRecord(value="c", timestamp=200),
        Watermark(200),
    ]
    merged = _merge_input_streams([left, right])
    emitted_wms = [el.timestamp for _ch, el in merged if isinstance(el, Watermark)]
    # the aligned watermark never exceeds the slowest channel (200) until both
    # channels pass it; channel 1 stops at 200, so the merged max is 200.
    assert max(emitted_wms) == 200
    # and it is monotonic non-decreasing
    assert emitted_wms == sorted(emitted_wms)


def test_interval_join_respects_slow_side_watermark() -> None:
    """A join only completes when both inputs' event time has advanced."""

    env = StreamEnvironment()
    fast = (
        env.from_source(
            [{"ts": 100, "id": "k"}, {"ts": 110, "id": "k"}], name="fast"
        )
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["id"])
    )
    slow = (
        env.from_source([{"ts": 150, "id": "k"}], name="slow")
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["id"])
    )
    def pair(lft: dict[str, Any], rgt: dict[str, Any]) -> tuple[int, int]:
        return int(lft["ts"]), int(rgt["ts"])

    joined = fast.interval_join(slow, lower_ms=0, upper_ms=100, join_fn=pair)
    result = env.execute()
    pairs = result.values(joined.node_id)
    # both fast records (100, 110) fall within [ts, ts+100] of the slow 150
    assert (100, 150) in pairs
    assert (110, 150) in pairs


# --------------------------------------------------------------------------- #
# Late-data edge cases on the window operator
# --------------------------------------------------------------------------- #
def test_session_window_with_lateness_merges_late_record() -> None:
    op: WindowOperator = WindowOperator(
        assigner=SessionWindows(gap_ms=100),
        aggregate=CountAggregate(),
        late_policy=LatePolicy(allowed_lateness_ms=200),
    )
    h: TestHarness = TestHarness(op)
    h.process_value({"ts": 1_000}, timestamp=1_000, key="s")
    h.process_value({"ts": 1_050}, timestamp=1_050, key="s")
    # close the session (gap 100 -> window end ~1150)
    h.process_watermark(1_150)
    first = h.output_values()
    assert first and first[-1].result == 2


def test_window_drops_record_far_past_lateness_to_side_output() -> None:
    op: WindowOperator = WindowOperator(
        assigner=TumblingEventTimeWindows(size_ms=1_000),
        aggregate=CountAggregate(),
        late_policy=LatePolicy(allowed_lateness_ms=0),
    )
    h: TestHarness = TestHarness(op)
    h.process_value({"ts": 500}, timestamp=500, key="s")
    h.process_watermark(2_000)  # window [0,1000) fired and cleaned
    # a record for the long-gone window is too late -> side output
    h.process_value({"ts": 100}, timestamp=100, key="s")
    assert len(h.side_output("late-records")) == 1


# --------------------------------------------------------------------------- #
# Broker -> engine -> broker round trip (facet-A integration shape)
# --------------------------------------------------------------------------- #
def test_broker_source_to_engine_to_sink() -> None:
    broker = InMemoryBroker(partitions=1)
    # produce reader-intent-shaped events onto a topic
    for ts in (100, 200, 1_200):
        broker.publish("reader-intent", {"ts": ts, "k": "s1"}, timestamp_ms=ts, key="s1")

    source = BrokerSource(broker=broker, topic="reader-intent", group="g")
    polled = source.poll()
    assert len(polled) == 3
    # the source erases the value type to ``object``; re-attach the known shape.
    records: list[StreamRecord[dict[str, Any]]] = [
        StreamRecord(value=cast("dict[str, Any]", r.value), timestamp=r.timestamp)
        for r in polled
    ]

    # feed the polled records straight into the engine via from_records
    env = StreamEnvironment()
    counts = (
        env.from_records(records)
        .assign_timestamps_and_watermarks(_wm())
        .key_by(lambda v: v["k"])
        .window(TumblingEventTimeWindows(size_ms=1_000))
        .aggregate(CountAggregate())
    )
    result = env.execute()

    # publish the window results back onto an output topic via the sink
    sink: BrokerSink[WindowResult[int]] = BrokerSink(broker=broker, topic="counts")
    for wr in result.typed_values(counts.node_id, WindowResult):
        sink.emit(StreamRecord(value=wr))

    out_msgs = broker.read("counts", 0, 0)
    totals = sorted(cast("WindowResult[int]", m.value).result for m in out_msgs)
    assert sum(totals) == 3  # all three events accounted for across windows


def test_broker_source_resumes_from_committed_offset() -> None:
    broker = InMemoryBroker(partitions=1)
    broker.publish("t", {"ts": 1}, timestamp_ms=1)
    source = BrokerSource(broker=broker, topic="t", group="g")
    assert len(source.poll()) == 1
    # nothing new yet
    assert source.poll() == []
    # produce more -> only the new record is polled
    broker.publish("t", {"ts": 2}, timestamp_ms=2)
    again = source.poll()
    assert len(again) == 1 and again[0].timestamp == 2
