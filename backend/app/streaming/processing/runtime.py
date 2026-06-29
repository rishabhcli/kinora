"""The execution engine — deterministic, single-threaded, checkpoint-aware.

Given a :class:`StreamGraph`, seed records per source, and watermark strategies,
the :class:`JobExecutor` runs the topology node by node in topological order. For
each node it builds the node's **input element stream** — the time-ordered merge
of every parent node's output stream (records interleaved with watermarks) — and
pushes it through the node's operator, capturing the operator's output stream for
that node's own children.

Two details make this both correct and Flink-faithful:

* **Watermark alignment.** A node's effective watermark is the *minimum* across
  its input channels (a node may not advance event time past the slowest input).
  The merge tracks each parent's latest watermark and only emits an aligned
  watermark to the operator when the channel minimum advances — exactly Flink's
  multi-input watermark rule. For a single-input node this reduces to "pass the
  parent's watermarks through".

* **Two-input tagging.** A join node's two parents feed one operator; the left
  parent's records are wrapped as ``TaggedRecord(is_left=True)`` and the right's
  as ``is_left=False`` before the operator sees them. A stream-table join routes
  its dimension parent into ``update_table`` instead.

The whole thing is pure and synchronous: no threads, no wall-clock, no I/O. Feed
it the same input twice and you get byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar, cast

from app.streaming.processing.dag import StreamGraph, StreamNode
from app.streaming.processing.joins import StreamTableJoinOperator, TaggedRecord
from app.streaming.processing.operators import Collector, Operator, RuntimeContext
from app.streaming.processing.records import (
    MAX_WATERMARK,
    MIN_WATERMARK,
    StreamElement,
    StreamRecord,
    Watermark,
)
from app.streaming.processing.state import (
    CheckpointCoordinator,
    CheckpointStorage,
    InMemoryCheckpointStorage,
    KeyedStateBackend,
)
from app.streaming.processing.time_domain import EventTimeTimerService, WatermarkStrategy

V = TypeVar("V")


@dataclass(slots=True)
class ExecutionResult:
    """Everything a run produced, addressable by node id.

    ``outputs`` is each node's primary record stream; ``side_outputs`` is its
    named side channels (late records, splits); ``checkpoints`` lists the
    completed checkpoint ids. Helpers pull values out without the StreamRecord
    wrapper for terse assertions.
    """

    job_name: str
    outputs: dict[str, list[StreamRecord[object]]] = field(default_factory=dict)
    side_outputs: dict[str, dict[str, list[StreamRecord[object]]]] = field(default_factory=dict)
    watermarks: dict[str, list[int]] = field(default_factory=dict)
    checkpoints: list[int] = field(default_factory=list)

    def records(self, node_id: str) -> list[StreamRecord[object]]:
        return self.outputs.get(node_id, [])

    def values(self, node_id: str) -> list[object]:
        return [r.value for r in self.outputs.get(node_id, [])]

    def typed_values(self, node_id: str, _type: type[V]) -> list[V]:
        """Like :meth:`values` but statically typed as ``list[_type]``.

        The element type is erased at the runtime boundary (every record value
        is ``object``); a topology author knows the concrete type a node emits,
        so this re-attaches it for terse, type-checked downstream code and tests.
        """

        return [cast(V, r.value) for r in self.outputs.get(node_id, [])]

    def side_output(self, node_id: str, tag: str) -> list[StreamRecord[object]]:
        return self.side_outputs.get(node_id, {}).get(tag, [])


@dataclass(slots=True)
class _NodeRuntime:
    """Per-node execution state."""

    node: StreamNode
    operator: Operator
    backend: KeyedStateBackend
    timers: EventTimeTimerService
    collector: Collector[object]
    ctx: RuntimeContext
    # the node's full output element stream (records + watermarks), in order.
    out_stream: list[StreamElement[object]] = field(default_factory=list)
    current_watermark: int = MIN_WATERMARK


def _merge_input_streams(
    parent_streams: list[list[StreamElement[object]]],
) -> list[tuple[int, StreamElement[object]]]:
    """Merge parent output streams into one channel-aligned input stream.

    Records pass through tagged with their *channel index* (parent ordinal).
    Watermarks are translated to the running *channel minimum*: a node may only
    advance to the lowest watermark seen across all its input channels. The
    output is a list of ``(channel_index, element)`` so the caller can tag
    two-input records by side.

    Determinism: parents are concatenated in node order, then stably sorted by
    the element's effective timestamp (records use their event time; watermarks
    use their value), so an out-of-order interleave is normalized the same way
    every run.
    """

    n = len(parent_streams)
    # Flatten with channel index and a stable position for tie-breaking.
    tagged: list[tuple[int, int, int, StreamElement[object]]] = []
    for ch, stream in enumerate(parent_streams):
        for pos, element in enumerate(stream):
            ts = element.timestamp
            tagged.append((ts, ch, pos, element))
    # sort by (timestamp, channel, position) — fully deterministic
    tagged.sort(key=lambda t: (t[0], t[1], t[2]))

    channel_wm = [MIN_WATERMARK] * n
    merged: list[tuple[int, StreamElement[object]]] = []
    last_emitted_wm = MIN_WATERMARK
    for _ts, ch, _pos, element in tagged:
        if isinstance(element, Watermark):
            channel_wm[ch] = element.timestamp
            aligned = min(channel_wm)
            if aligned > last_emitted_wm:
                merged.append((ch, Watermark(aligned)))
                last_emitted_wm = aligned
        else:
            merged.append((ch, element))
    return merged


class JobExecutor:
    """Runs a :class:`StreamGraph` to completion, deterministically."""

    def __init__(
        self,
        *,
        graph: StreamGraph,
        source_records: dict[str, list[StreamRecord[object]]],
        watermark_strategies: dict[str, WatermarkStrategy[object]],
        checkpoint_storage: CheckpointStorage | None = None,
        checkpoint_every: int | None = None,
    ) -> None:
        self._graph = graph
        self._source_records = source_records
        self._watermarks = watermark_strategies
        self._storage = checkpoint_storage or InMemoryCheckpointStorage()
        self._checkpoint_every = checkpoint_every
        self._coordinator = CheckpointCoordinator(storage=self._storage)
        self._processed = 0

    # -- public API -------------------------------------------------------- #
    def run(self, *, name: str = "kinora-stream-job") -> ExecutionResult:
        order = self._graph.topological_order()
        runtimes = self._build_runtimes(order)
        result = ExecutionResult(job_name=name)
        self._processed = 0

        for node in order:
            rt = runtimes[node.node_id]
            if node.node_id in self._source_records:
                self._run_source(rt, result)
            else:
                self._run_operator(rt, runtimes, result)

        result.checkpoints.append(self._coordinator.trigger())
        result.checkpoints = sorted(set(result.checkpoints))
        return result

    @property
    def coordinator(self) -> CheckpointCoordinator:
        return self._coordinator

    # -- construction ------------------------------------------------------ #
    def _build_runtimes(self, order: list[StreamNode]) -> dict[str, _NodeRuntime]:
        runtimes: dict[str, _NodeRuntime] = {}
        for node in order:
            operator = node.build()
            backend = KeyedStateBackend(node.node_id)
            timers = EventTimeTimerService()
            collector: Collector[object] = Collector()
            ctx = RuntimeContext(
                operator_id=node.node_id,
                state=backend,
                timers=timers,
                collector=collector,
                current_watermark=MIN_WATERMARK,
            )
            operator.open(ctx)
            self._coordinator.register(backend)
            runtimes[node.node_id] = _NodeRuntime(
                node=node,
                operator=operator,
                backend=backend,
                timers=timers,
                collector=collector,
                ctx=ctx,
            )
        return runtimes

    def _materialize_source(self, node_id: str) -> list[StreamElement[object]]:
        """Interleave watermarks into a source's records per its strategy."""

        records = self._source_records[node_id]
        strategy = self._watermarks.get(node_id)
        elements: list[StreamElement[object]] = []
        if strategy is None:
            elements.extend(records)
            elements.append(Watermark(MAX_WATERMARK))
            return elements

        gen = strategy.generator_factory()
        last_emitted = MIN_WATERMARK
        for record in records:
            ts = strategy.assign(record)
            stamped = record.with_timestamp(ts)
            gen.on_event(ts)
            elements.append(stamped)
            wm = gen.current_watermark()
            if wm > last_emitted:
                elements.append(Watermark(wm))
                last_emitted = wm
        elements.append(Watermark(MAX_WATERMARK))
        return elements

    # -- execution per node ------------------------------------------------ #
    def _run_source(self, rt: _NodeRuntime, result: ExecutionResult) -> None:
        stream = self._materialize_source(rt.node.node_id)
        for element in stream:
            if isinstance(element, Watermark):
                rt.out_stream.append(element)
            else:
                rt.ctx.current_watermark = rt.current_watermark
                rt.operator.process_record(element)
                self._capture(rt, result)
        rt.operator.close()
        self._capture(rt, result)

    def _run_operator(
        self,
        rt: _NodeRuntime,
        runtimes: dict[str, _NodeRuntime],
        result: ExecutionResult,
    ) -> None:
        parent_streams = [runtimes[p].out_stream for p in rt.node.parents]
        merged = _merge_input_streams(parent_streams)
        two_input = len(rt.node.parents) == 2
        is_table_join = isinstance(rt.operator, StreamTableJoinOperator)
        # A two-input operator opts into per-side tagging via ``wants_tagged_input``
        # (interval join, co-process). A union does not — it gets raw records.
        wants_tagged = bool(getattr(rt.operator, "wants_tagged_input", False))

        for ch, element in merged:
            if isinstance(element, Watermark):
                if element.timestamp <= rt.current_watermark and element.timestamp != MAX_WATERMARK:
                    continue
                rt.current_watermark = element.timestamp
                rt.ctx.current_watermark = element.timestamp
                rt.operator.process_watermark(element)
                result.watermarks.setdefault(rt.node.node_id, []).append(element.timestamp)
                self._capture(rt, result, emit_watermark=element)
            else:
                rt.ctx.current_watermark = rt.current_watermark
                self._feed_record(rt, ch, element, two_input, is_table_join, wants_tagged)
                self._capture(rt, result)
                self._processed += 1
                if self._checkpoint_every and self._processed % self._checkpoint_every == 0:
                    result.checkpoints.append(self._coordinator.trigger())

        rt.operator.close()
        self._capture(rt, result)

    def _feed_record(
        self,
        rt: _NodeRuntime,
        channel: int,
        record: StreamRecord[object],
        two_input: bool,
        is_table_join: bool,
        wants_tagged: bool,
    ) -> None:
        if is_table_join:
            # channel 0 = fact stream, channel 1 = dimension table updates
            if channel == 0:
                rt.operator.process_record(record)
            else:
                rt.operator.update_table(record.value)  # type: ignore[attr-defined]
            return
        if two_input and wants_tagged:
            tagged = (
                TaggedRecord(is_left=True, left=record.value)
                if channel == 0
                else TaggedRecord(is_left=False, right=record.value)
            )
            rt.operator.process_record(record.with_value(tagged))
            return
        rt.operator.process_record(record)

    # -- output capture ---------------------------------------------------- #
    def _capture(
        self,
        rt: _NodeRuntime,
        result: ExecutionResult,
        *,
        emit_watermark: Watermark | None = None,
    ) -> None:
        primary = rt.collector.drain()
        side = rt.collector.drain_side()
        if primary:
            result.outputs.setdefault(rt.node.node_id, []).extend(primary)
            rt.out_stream.extend(primary)
        for tag, recs in side.items():
            result.side_outputs.setdefault(rt.node.node_id, {}).setdefault(tag, []).extend(recs)
        if emit_watermark is not None:
            rt.out_stream.append(emit_watermark)
