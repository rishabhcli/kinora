"""The fluent DataStream API — builds a :class:`StreamGraph` declaratively.

This is the surface a pipeline author writes against, modelled on Flink's Java /
Python DataStream API:

    env = StreamEnvironment()
    stream = (
        env.from_source(records)
        .assign_timestamps_and_watermarks(strategy)
        .filter(lambda e: e.kind == "scroll")
        .map(to_velocity)
        .key_by(lambda e: e.session_id)
        .window(TumblingEventTimeWindows(10_000))
        .aggregate(MeanAggregate(lambda e: e.velocity_wps), allowed_lateness_ms=2_000)
    )
    result = env.execute()

Each call appends a :class:`StreamNode` to the environment's graph and returns a
new :class:`DataStream` / :class:`KeyedStream` / :class:`WindowedStream` handle
pointing at the new node. Nothing executes until :meth:`StreamEnvironment.execute`
runs the graph through the :class:`JobExecutor`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Generic, TypeVar

from app.streaming.processing.aggregations import AggregateFunction, ReduceFunction
from app.streaming.processing.dag import StreamGraph, new_node
from app.streaming.processing.joins import (
    IntervalJoinOperator,
    StreamTableJoinOperator,
)
from app.streaming.processing.operators import (
    CoProcessFunction,
    FilterOperator,
    FlatMapOperator,
    KeyByOperator,
    MapOperator,
    Operator,
    ProcessFunction,
    SplitOperator,
    UnionOperator,
)
from app.streaming.processing.records import LatePolicy, StreamRecord
from app.streaming.processing.time_domain import WatermarkStrategy
from app.streaming.processing.window_operator import WindowOperator, WindowResult
from app.streaming.processing.windows import WindowAssigner

if TYPE_CHECKING:
    from app.streaming.processing.runtime import ExecutionResult

T = TypeVar("T")
U = TypeVar("U")
K = TypeVar("K")
R = TypeVar("R")
ACC = TypeVar("ACC")
OUT = TypeVar("OUT")


class _SourceOperator(Generic[T]):
    """Forwards its seeded records into the DAG.

    The runtime materializes the source's element stream (records interleaved
    with watermarks) and feeds it in; the operator simply re-emits each record
    to its collector so the engine can route it to children. Watermarks are
    handled by the runtime directly (propagated to children), so the operator's
    ``process_watermark`` is a no-op.
    """

    def __init__(self, records: list[StreamRecord[T]]) -> None:
        self.records = records
        self._collector: object | None = None

    def open(self, ctx: object) -> None:
        self._collector = getattr(ctx, "collector", None)

    def process_record(self, record: StreamRecord[T]) -> None:
        if self._collector is not None:
            self._collector.collect(record)  # type: ignore[attr-defined]

    def process_watermark(self, watermark: object) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def is_keyed(self) -> bool:
        return False


class StreamEnvironment:
    """The job builder + entry point.

    Holds the :class:`StreamGraph`, the per-source seed records, and the
    per-source watermark strategy. :meth:`execute` hands all three to the
    :class:`JobExecutor`.
    """

    def __init__(self) -> None:
        self.graph = StreamGraph()
        self._source_records: dict[str, list[StreamRecord[object]]] = {}
        self._watermarks: dict[str, WatermarkStrategy[object]] = {}

    def from_source(self, values: Iterable[T], *, name: str = "source") -> DataStream[T]:
        """Create a source from raw values (each stamped ``NO_TIMESTAMP``).

        Attach a :meth:`DataStream.assign_timestamps_and_watermarks` strategy to
        pull the event time out of each value. For pre-built
        :class:`StreamRecord`\\ s (e.g. a broker poll) use :meth:`from_records`.
        """

        materialized: list[StreamRecord[object]] = [StreamRecord(value=v) for v in values]
        return self._add_source(materialized, name)

    def from_records(
        self, records: Iterable[StreamRecord[T]], *, name: str = "source"
    ) -> DataStream[T]:
        """Create a source from pre-built records (value + timestamp + key).

        Used to feed records straight off a broker poll
        (:class:`~app.streaming.processing.broker.BrokerSource`) without losing
        their already-assigned timestamps.
        """

        materialized: list[StreamRecord[object]] = [
            StreamRecord(value=r.value, timestamp=r.timestamp, key=r.key) for r in records
        ]
        return self._add_source(materialized, name)

    def _add_source(
        self, materialized: list[StreamRecord[object]], name: str
    ) -> DataStream[T]:
        node = new_node(name, lambda: _SourceOperator(materialized), prefix="src")
        self.graph.add(node, is_source=True)
        self._source_records[node.node_id] = materialized
        return DataStream(self, node.node_id)

    def execute(self, *, name: str = "kinora-stream-job") -> ExecutionResult:
        from app.streaming.processing.runtime import JobExecutor

        executor = JobExecutor(
            graph=self.graph,
            source_records=self._source_records,
            watermark_strategies=self._watermarks,
        )
        return executor.run(name=name)

    # internal: register a watermark strategy against a source node
    def _set_watermark(self, node_id: str, strategy: WatermarkStrategy[object]) -> None:
        self._watermarks[node_id] = strategy


class DataStream(Generic[T]):
    """A non-keyed stream handle. Supports stateless transforms + keying."""

    def __init__(self, env: StreamEnvironment, node_id: str) -> None:
        self._env = env
        self._node_id = node_id

    @property
    def node_id(self) -> str:
        return self._node_id

    def _append(
        self, name: str, factory: Callable[[], Operator], *, keyed: bool = False
    ) -> str:
        node = new_node(name, factory, parents=[self._node_id], keyed=keyed)
        self._env.graph.add(node)
        return node.node_id

    # -- timestamps / watermarks ------------------------------------------- #
    def assign_timestamps_and_watermarks(self, strategy: WatermarkStrategy[T]) -> DataStream[T]:
        """Attach a watermark strategy. Must be applied on a source stream."""

        self._env._set_watermark(self._node_id, strategy)  # type: ignore[arg-type]
        return self

    # -- stateless transforms ---------------------------------------------- #
    def map(self, fn: Callable[[T], U]) -> DataStream[U]:
        nid = self._append("map", lambda: MapOperator(fn))
        return DataStream(self._env, nid)

    def filter(self, predicate: Callable[[T], bool]) -> DataStream[T]:
        nid = self._append("filter", lambda: FilterOperator(predicate))
        return DataStream(self._env, nid)

    def flat_map(self, fn: Callable[[T], Iterable[U]]) -> DataStream[U]:
        nid = self._append("flat_map", lambda: FlatMapOperator(fn))
        return DataStream(self._env, nid)

    def split(self, tag_selector: Callable[[T], str]) -> DataStream[T]:
        nid = self._append("split", lambda: SplitOperator(tag_selector))
        return DataStream(self._env, nid)

    # -- keying ------------------------------------------------------------ #
    def key_by(self, key_selector: Callable[[T], object]) -> KeyedStream[T]:
        nid = self._append("key_by", lambda: KeyByOperator(key_selector))
        return KeyedStream(self._env, nid)

    # -- union ------------------------------------------------------------- #
    def union(self, other: DataStream[T]) -> DataStream[T]:
        """Merge this stream with ``other`` (same type) into one, by event time.

        Both inputs feed a pass-through :class:`UnionOperator`; the runtime
        interleaves the two parents' records by timestamp, so the result is a
        true event-time merge — not a concatenation.
        """

        node = new_node(
            "union",
            UnionOperator,
            parents=[self._node_id, other.node_id],
            keyed=False,
        )
        self._env.graph.add(node)
        return DataStream(self._env, node.node_id)


class KeyedStream(Generic[T]):
    """A keyed stream: keyed process functions, windowing, joins."""

    def __init__(self, env: StreamEnvironment, node_id: str) -> None:
        self._env = env
        self._node_id = node_id

    @property
    def node_id(self) -> str:
        return self._node_id

    def _append_keyed(
        self, name: str, factory: Callable[[], Operator]
    ) -> str:
        node = new_node(name, factory, parents=[self._node_id], keyed=True)
        self._env.graph.add(node)
        return node.node_id

    # stateless transforms stay keyed (key is preserved by these operators)
    def map(self, fn: Callable[[T], U]) -> KeyedStream[U]:
        nid = self._append_keyed("map", lambda: MapOperator(fn))
        return KeyedStream(self._env, nid)

    def filter(self, predicate: Callable[[T], bool]) -> KeyedStream[T]:
        nid = self._append_keyed("filter", lambda: FilterOperator(predicate))
        return KeyedStream(self._env, nid)

    def process(self, fn_factory: Callable[[], ProcessFunction[T, U]]) -> DataStream[U]:
        """Attach a keyed :class:`ProcessFunction` (state + timers + side out)."""

        nid = self._append_keyed("process", fn_factory)
        return DataStream(self._env, nid)

    def window(self, assigner: WindowAssigner) -> WindowedStream[T]:
        return WindowedStream(self._env, self._node_id, assigner)

    def interval_join(
        self,
        other: KeyedStream[R],
        *,
        lower_ms: int,
        upper_ms: int,
        join_fn: Callable[[T, R], OUT],
    ) -> DataStream[OUT]:
        """Stream-stream interval join (see :class:`IntervalJoinOperator`).

        Both inputs feed a single two-input operator; the runtime tags each side
        and merges them by event time before the operator sees them.
        """

        def factory() -> Operator:
            return IntervalJoinOperator(
                lower_ms=lower_ms, upper_ms=upper_ms, join_fn=join_fn
            )

        node = new_node(
            "interval_join",
            factory,
            parents=[self._node_id, other.node_id],
            keyed=True,
        )
        self._env.graph.add(node)
        return DataStream(self._env, node.node_id)

    def join_table(
        self,
        table_stream: KeyedStream[R],
        *,
        join_fn: Callable[[T, R | None], OUT | None],
        table_key_selector: Callable[[R], object],
    ) -> DataStream[OUT]:
        """Stream-table enrichment join (see :class:`StreamTableJoinOperator`).

        ``table_stream`` is the slowly-changing dimension; its records build a
        last-write-wins table the main stream is enriched against.
        """

        def factory() -> Operator:
            return StreamTableJoinOperator(
                join_fn=join_fn, table_key_selector=table_key_selector
            )

        node = new_node(
            "stream_table_join",
            factory,
            parents=[self._node_id, table_stream.node_id],
            keyed=True,
        )
        self._env.graph.add(node)
        return DataStream(self._env, node.node_id)

    def connect(
        self,
        other: KeyedStream[R],
        fn_factory: Callable[[], CoProcessFunction[OUT]],
    ) -> DataStream[OUT]:
        """Attach a two-input keyed :class:`CoProcessFunction` over both streams.

        Both inputs share one keyed state + timer service. The runtime tags each
        record by side and dispatches to ``process_left`` / ``process_right``.
        The general two-input primitive behind interval/table joins.
        """

        node = new_node(
            "co_process",
            fn_factory,
            parents=[self._node_id, other.node_id],
            keyed=True,
        )
        self._env.graph.add(node)
        return DataStream(self._env, node.node_id)


class WindowedStream(Generic[T]):
    """A keyed stream with a window assigner applied; awaiting an aggregation."""

    def __init__(
        self, env: StreamEnvironment, node_id: str, assigner: WindowAssigner
    ) -> None:
        self._env = env
        self._node_id = node_id
        self._assigner = assigner

    def aggregate(
        self,
        aggregate: AggregateFunction[T, ACC, OUT],
        *,
        allowed_lateness_ms: int = 0,
        trigger: object | None = None,
    ) -> DataStream[WindowResult[OUT]]:
        late = LatePolicy(allowed_lateness_ms=allowed_lateness_ms)

        def factory() -> Operator:
            return WindowOperator(
                assigner=self._assigner,
                aggregate=aggregate,
                trigger=trigger,  # type: ignore[arg-type]
                late_policy=late,
            )

        node = new_node("window", factory, parents=[self._node_id], keyed=True)
        self._env.graph.add(node)
        return DataStream(self._env, node.node_id)

    def reduce(
        self, reduce_fn: ReduceFunction[T], *, allowed_lateness_ms: int = 0
    ) -> DataStream[WindowResult[T]]:
        """Window reduction via a binary reduce function (``IN`` == ``OUT``)."""

        class _ReduceAgg:
            def create_accumulator(self) -> T | None:
                return None

            def add(self, value: T, acc: T | None) -> T:
                return value if acc is None else reduce_fn(acc, value)

            def get_result(self, acc: T | None) -> T:
                assert acc is not None
                return acc

            def merge(self, a: T, b: T) -> T:
                return reduce_fn(a, b)

        return self.aggregate(_ReduceAgg(), allowed_lateness_ms=allowed_lateness_ms)  # type: ignore[arg-type]
