"""Change-data-capture + incrementally-maintained materialised views (facet C).

A self-contained streaming data plane:

* **Sources** — :class:`PostgresLogicalSource` (WAL / logical replication),
  :class:`PollingSource` (updated-at high-watermark + tombstone fallback), and
  :class:`FakeChangeStream` (deterministic test stream). All implement the
  :class:`CDCSource` contract and emit typed :class:`ChangeEvent`\\ s.
* **Bootstrap** — :class:`SnapshotCoordinator` does the consistent
  snapshot → stream cutover; :class:`SchemaRegistry` handles schema evolution.
* **Sinks** — :class:`ChangeSink` protocol with :class:`InMemorySink`,
  :class:`RedisStreamSink`, :class:`FanoutSink`, and :class:`BrokerSink` (a
  duck-typed adapter for sibling facet A's ``Broker``).
* **Views** — :class:`MaterializedViewEngine` maintains denormalised read models
  (:class:`LibraryShelfView`, :class:`CanonGraphView`) incrementally from the
  change stream, with a consistency oracle.
* **Pipeline** — :class:`CDCPipeline` wires source → migrate → dedup → sink with
  resumable :class:`OffsetStore` checkpoints.

See ``DESIGN.md`` for the architecture and roadmap.
"""

from __future__ import annotations

from app.streaming.cdc.clock import Clock, FakeClock, SystemClock
from app.streaming.cdc.events import ChangeEvent, JsonRow, LogPosition, Op, key_str
from app.streaming.cdc.metrics import CdcMetrics, MeteredSink, TableMetrics
from app.streaming.cdc.offsets import DbOffsetStore, InMemoryOffsetStore, OffsetStore
from app.streaming.cdc.pipeline import CDCPipeline, PipelineResult
from app.streaming.cdc.polling_source import (
    ListRowFetcher,
    PollCursor,
    PollingSource,
    RowFetcher,
)
from app.streaming.cdc.runner import CDCRunner, RunnerResult, build_kinora_views
from app.streaming.cdc.schema import (
    Column,
    SchemaDelta,
    SchemaRegistry,
    TableSchema,
    migrate_row,
    reconcile,
)
from app.streaming.cdc.service import ViewNotFoundError, ViewReadService
from app.streaming.cdc.sink import (
    BrokerLike,
    BrokerSink,
    ChangeSink,
    FanoutSink,
    InMemorySink,
    NullSink,
    RedisStreamSink,
)
from app.streaming.cdc.snapshot import (
    SnapshotCoordinator,
    SnapshotProgress,
    SnapshotState,
)
from app.streaming.cdc.source import CDCSource, FakeChangeStream, ReplayBuffer
from app.streaming.cdc.views import (
    AggregateView,
    AvgReducer,
    CanonGraphView,
    CountReducer,
    Delta,
    DependencyGraph,
    DistinctCountReducer,
    EquiJoinView,
    KeyedProjectionView,
    LibraryShelfView,
    MaterializedView,
    MaterializedViewEngine,
    MaxReducer,
    MinReducer,
    Reducer,
    Row,
    SumReducer,
    ViewConsistency,
    ZSet,
)
from app.streaming.cdc.wal_source import (
    ListWalReader,
    PostgresLogicalSource,
    WalReader,
    decode_wal2json,
    parse_lsn,
)

__all__ = [
    # clock
    "Clock",
    "FakeClock",
    "SystemClock",
    # events
    "ChangeEvent",
    "JsonRow",
    "LogPosition",
    "Op",
    "key_str",
    # sources
    "CDCSource",
    "FakeChangeStream",
    "ReplayBuffer",
    "PollingSource",
    "PollCursor",
    "RowFetcher",
    "ListRowFetcher",
    "PostgresLogicalSource",
    "WalReader",
    "ListWalReader",
    "decode_wal2json",
    "parse_lsn",
    # bootstrap / schema
    "SnapshotCoordinator",
    "SnapshotProgress",
    "SnapshotState",
    "SchemaRegistry",
    "TableSchema",
    "Column",
    "SchemaDelta",
    "reconcile",
    "migrate_row",
    # sinks
    "ChangeSink",
    "InMemorySink",
    "FanoutSink",
    "BrokerSink",
    "BrokerLike",
    "RedisStreamSink",
    "NullSink",
    # offsets
    "OffsetStore",
    "InMemoryOffsetStore",
    "DbOffsetStore",
    # metrics
    "CdcMetrics",
    "MeteredSink",
    "TableMetrics",
    # runner
    "CDCRunner",
    "RunnerResult",
    "build_kinora_views",
    # read service
    "ViewReadService",
    "ViewNotFoundError",
    # views
    "MaterializedView",
    "MaterializedViewEngine",
    "ViewConsistency",
    "DependencyGraph",
    "KeyedProjectionView",
    "AggregateView",
    "EquiJoinView",
    "Reducer",
    "CountReducer",
    "SumReducer",
    "AvgReducer",
    "MinReducer",
    "MaxReducer",
    "DistinctCountReducer",
    "LibraryShelfView",
    "CanonGraphView",
    "Row",
    "ZSet",
    "Delta",
    # pipeline
    "CDCPipeline",
    "PipelineResult",
]
