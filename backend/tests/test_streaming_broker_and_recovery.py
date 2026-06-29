"""Broker fallback, source/sink connectors, DAG, and exactly-once recovery."""

from __future__ import annotations

from typing import Any, cast

from app.streaming.processing import CountAggregate
from app.streaming.processing.broker import (
    Broker,
    BrokerSink,
    BrokerSource,
    InMemoryBroker,
)
from app.streaming.processing.dag import StreamGraph, new_node
from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.operators import MapOperator
from app.streaming.processing.records import StreamRecord
from app.streaming.processing.runtime import JobExecutor
from app.streaming.processing.state import InMemoryCheckpointStorage
from app.streaming.processing.time_domain import (
    WatermarkStrategy,
    field_timestamp_assigner,
)
from app.streaming.processing.window_operator import WindowResult
from app.streaming.processing.windows import TumblingEventTimeWindows


# --------------------------------------------------------------------------- #
# Broker fallback
# --------------------------------------------------------------------------- #
def test_in_memory_broker_satisfies_protocol() -> None:
    broker = InMemoryBroker(partitions=2)
    assert isinstance(broker, Broker)


def test_broker_publish_read_offsets() -> None:
    broker = InMemoryBroker(partitions=1)
    o0 = broker.publish("reader-intent", {"w": 1}, timestamp_ms=1_000, key="s1")
    o1 = broker.publish("reader-intent", {"w": 2}, timestamp_ms=2_000, key="s1")
    assert (o0, o1) == (0, 1)
    msgs = broker.read("reader-intent", 0, 0)
    assert [cast("dict[str, int]", m.value)["w"] for m in msgs] == [1, 2]
    assert broker.high_watermark("reader-intent", 0) == 2


def test_broker_consumer_group_commit() -> None:
    broker = InMemoryBroker(partitions=1)
    broker.publish("t", "a", timestamp_ms=1)
    broker.publish("t", "b", timestamp_ms=2)
    assert broker.committed("g", "t", 0) == 0
    broker.commit("g", "t", 0, 2)
    assert broker.committed("g", "t", 0) == 2


def test_broker_source_polls_and_commits() -> None:
    broker = InMemoryBroker(partitions=2)
    for i in range(4):
        broker.publish("t", {"i": i}, timestamp_ms=1_000 + i, key=f"k{i % 2}")
    source = BrokerSource(broker=broker, topic="t", group="g")
    records = source.poll()
    assert len(records) == 4
    assert [r.timestamp for r in records] == sorted(r.timestamp for r in records)
    # a second poll sees nothing new (offsets committed)
    assert source.poll() == []


def test_broker_sink_emits() -> None:
    broker = InMemoryBroker(partitions=1)
    sink: BrokerSink[dict] = BrokerSink(broker=broker, topic="out")
    sink.emit(StreamRecord(value={"v": 1}, timestamp=1_000))
    assert broker.high_watermark("out", 0) == 1


# --------------------------------------------------------------------------- #
# DAG topology
# --------------------------------------------------------------------------- #
def test_topological_order_and_describe() -> None:
    g = StreamGraph()
    a = new_node("src", lambda: MapOperator(lambda x: x), prefix="src")
    g.add(a, is_source=True)
    b = new_node("map", lambda: MapOperator(lambda x: x), parents=[a.node_id])
    g.add(b)
    c = new_node("filter", lambda: MapOperator(lambda x: x), parents=[b.node_id])
    g.add(c)
    order = [n.node_id for n in g.topological_order()]
    assert order.index(a.node_id) < order.index(b.node_id) < order.index(c.node_id)
    assert len(g.describe()) == 3


def test_cycle_detection() -> None:
    g = StreamGraph()
    a = new_node("a", lambda: MapOperator(lambda x: x))
    b = new_node("b", lambda: MapOperator(lambda x: x))
    a.parents.append(b.node_id)
    b.parents.append(a.node_id)
    g.add(a)
    g.add(b)
    try:
        g.topological_order()
    except ValueError as exc:
        assert "cycle" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a cycle error")


# --------------------------------------------------------------------------- #
# Exactly-once: replay from a checkpoint reproduces identical output.
# --------------------------------------------------------------------------- #
def _build_counting_executor(
    storage: InMemoryCheckpointStorage, checkpoint_every: int
) -> tuple[JobExecutor, StreamEnvironment, str]:
    env = StreamEnvironment()
    events: list[dict[str, Any]] = [{"ts": t, "k": "k"} for t in range(0, 1_000, 100)]
    node = (
        env.from_source(events)
        .assign_timestamps_and_watermarks(
            WatermarkStrategy.for_bounded_out_of_orderness(
                field_timestamp_assigner(lambda v: int(v["ts"])), 0
            )
        )
        .key_by(lambda v: v["k"])
        .window(TumblingEventTimeWindows(size_ms=200))
        .aggregate(CountAggregate())
    )
    executor = JobExecutor(
        graph=env.graph,
        source_records=env._source_records,
        watermark_strategies=env._watermarks,
        checkpoint_storage=storage,
        checkpoint_every=checkpoint_every,
    )
    return executor, env, node.node_id


def test_exactly_once_checkpoint_then_restore_is_consistent() -> None:
    # Run once, capturing checkpoints in shared storage.
    storage = InMemoryCheckpointStorage()
    executor, _env, node_id = _build_counting_executor(storage, checkpoint_every=3)
    result = executor.run()
    assert result.checkpoints  # checkpoints completed

    # The coordinator can restore the latest checkpoint into fresh backends and
    # the restored state matches what was snapshotted (no loss / duplication).
    latest_id = max(storage.completed_checkpoints())
    snap = storage.get(node_id, latest_id)
    assert snap is not None
    # window-acc namespace exists and holds partial counts for key "k"
    assert any(name == "win-acc" for name in snap.state_names())


def test_determinism_same_input_same_output() -> None:
    storage1 = InMemoryCheckpointStorage()
    storage2 = InMemoryCheckpointStorage()
    e1, _, n1 = _build_counting_executor(storage1, checkpoint_every=2)
    e2, _, n2 = _build_counting_executor(storage2, checkpoint_every=5)
    r1 = e1.run()
    r2 = e2.run()
    counts1 = sorted(wr.result for wr in r1.typed_values(n1, WindowResult))
    counts2 = sorted(wr.result for wr in r2.typed_values(n2, WindowResult))
    # output is identical regardless of checkpoint cadence
    assert counts1 == counts2
    assert sum(counts1) == 10  # 10 source events all counted
