"""Facet B — a Flink / Kafka-Streams-shaped stateful stream processor.

The public surface mirrors the shape of a real streaming engine so the concepts
transfer 1:1:

* **Records & time** (:mod:`records`, :mod:`time_domain`) — a stream is a
  sequence of :class:`StreamRecord` carrying an *event-time* timestamp,
  interleaved with :class:`Watermark` markers that assert event-time progress.
* **Operators** (:mod:`operators`) — the transformation primitives:
  ``map`` / ``filter`` / ``flat_map`` / ``key_by`` / ``process`` — composed into
  a DAG.
* **DataStream / DAG** (:mod:`datastream`, :mod:`dag`) — the fluent topology
  builder and the immutable logical graph it produces.
* **Windowing** (:mod:`windows`, :mod:`triggers`) — tumbling / sliding / session
  assigners, event-time triggers, and **allowed-lateness** handling with a
  side-output for dropped-late records.
* **Aggregation & joins** (:mod:`aggregations`, :mod:`joins`) — incremental
  aggregate functions and reducing state, stream-stream **interval joins**, and
  stream-table (enrichment) joins.
* **State** (:mod:`state`) — a pluggable, **checkpointed** keyed-state backend
  (value / list / map / reducing / aggregating) with **exactly-once** snapshots
  via aligned barriers.
* **Runtime** (:mod:`runtime`) — the single-threaded, deterministic execution
  engine that pushes records through the DAG, propagates watermarks, fires
  timers and windows, and coordinates checkpoints.
* **Test driver** (:mod:`testkit`) — a deterministic event-time harness for
  asserting operator and pipeline output without any wall-clock.

Concrete Kinora pipelines live under :mod:`app.streaming.processing.pipelines`.
"""

from __future__ import annotations

from app.streaming.processing.aggregations import (
    AggregateFunction,
    CountAggregate,
    MaxAggregate,
    MeanAggregate,
    MinAggregate,
    ReduceFunction,
    SumAggregate,
)
from app.streaming.processing.datastream import (
    DataStream,
    KeyedStream,
    StreamEnvironment,
    WindowedStream,
)
from app.streaming.processing.joins import IntervalJoinOperator, StreamTableJoinOperator
from app.streaming.processing.operators import (
    CoProcessFunction,
    ProcessFunction,
    UnionOperator,
)
from app.streaming.processing.records import StreamElement, StreamRecord, Watermark
from app.streaming.processing.runtime import ExecutionResult, JobExecutor
from app.streaming.processing.state import (
    CheckpointStorage,
    InMemoryCheckpointStorage,
    KeyedStateBackend,
    Snapshot,
)
from app.streaming.processing.testkit import TestHarness, collect
from app.streaming.processing.time_domain import (
    BoundedOutOfOrdernessGenerator,
    MonotonousGenerator,
    TimerService,
    WatermarkStrategy,
    field_timestamp_assigner,
)
from app.streaming.processing.windows import (
    SessionWindows,
    SlidingEventTimeWindows,
    TimeWindow,
    TumblingEventTimeWindows,
)

__all__ = [
    "AggregateFunction",
    "BoundedOutOfOrdernessGenerator",
    "CheckpointStorage",
    "CoProcessFunction",
    "CountAggregate",
    "DataStream",
    "ExecutionResult",
    "InMemoryCheckpointStorage",
    "IntervalJoinOperator",
    "JobExecutor",
    "KeyedStateBackend",
    "KeyedStream",
    "MaxAggregate",
    "MeanAggregate",
    "MinAggregate",
    "MonotonousGenerator",
    "ProcessFunction",
    "ReduceFunction",
    "SessionWindows",
    "SlidingEventTimeWindows",
    "Snapshot",
    "StreamElement",
    "StreamEnvironment",
    "StreamRecord",
    "StreamTableJoinOperator",
    "SumAggregate",
    "TestHarness",
    "TimeWindow",
    "TimerService",
    "TumblingEventTimeWindows",
    "UnionOperator",
    "Watermark",
    "WatermarkStrategy",
    "WindowedStream",
    "collect",
    "field_timestamp_assigner",
]
