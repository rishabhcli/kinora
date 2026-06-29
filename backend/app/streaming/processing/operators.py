"""Stream operators — the transformation primitives of the DAG.

Every node in a Kinora streaming topology is an :class:`Operator`. The runtime
pushes records and watermarks into an operator and collects what it emits. The
contract is intentionally small and Flink-shaped:

* ``open(ctx)`` — bind the operator to its runtime context (state backend,
  timer service, output collector). Called once before any record.
* ``process_record(record)`` — handle one data record.
* ``process_watermark(watermark)`` — react to event-time progress: fire timers,
  emit ready windows.
* ``close()`` — flush at end-of-stream (after the MAX watermark).

Stateless transforms (:class:`MapOperator`, :class:`FilterOperator`,
:class:`FlatMapOperator`, :class:`KeyByOperator`) implement this directly.
Stateful operators (windowing, aggregation, joins, process functions) live in
:mod:`windows_operator`, :mod:`joins`, and use the keyed-state backend through
the :class:`RuntimeContext`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from app.streaming.processing.records import SideOutput, StreamRecord, Watermark
from app.streaming.processing.state import KeyedStateBackend
from app.streaming.processing.time_domain import EventTimeTimerService

IN = TypeVar("IN")
OUT = TypeVar("OUT")
T = TypeVar("T")


class Collector(Generic[OUT]):
    """Sink an operator emits into.

    ``collect`` emits to the primary output; ``collect_side`` emits to a named
    side output (late records, split branches). The runtime supplies the
    collector; operators never construct one.
    """

    def __init__(self) -> None:
        self.primary: list[StreamRecord[OUT]] = []
        self.side_outputs: dict[str, list[StreamRecord[object]]] = {}

    def collect(self, record: StreamRecord[OUT]) -> None:
        self.primary.append(record)

    def collect_value(self, value: OUT, timestamp: int, key: object | None = None) -> None:
        self.primary.append(StreamRecord(value=value, timestamp=timestamp, key=key))

    def collect_side(self, tag: str, record: StreamRecord[object]) -> None:
        self.side_outputs.setdefault(tag, []).append(record)

    def drain(self) -> list[StreamRecord[OUT]]:
        out = self.primary
        self.primary = []
        return out

    def drain_side(self) -> dict[str, list[StreamRecord[object]]]:
        out = self.side_outputs
        self.side_outputs = {}
        return out


@dataclass(slots=True)
class RuntimeContext:
    """Everything an operator needs from the engine at runtime.

    ``state`` is the operator's own keyed-state backend (key already scoped by
    the runtime before each keyed call); ``timers`` schedules event-time timers;
    ``collector`` is the output channel; ``current_watermark`` is the latest
    event-time the operator has observed.
    """

    operator_id: str
    state: KeyedStateBackend
    timers: EventTimeTimerService
    collector: Collector[object]
    current_watermark: int = 0


@runtime_checkable
class Operator(Protocol):
    """A node in the dataflow graph.

    Not parameterized: the runtime always holds operators as a heterogeneous
    list and erases element types at the boundary. Concrete operators
    (:class:`BaseOperator` subclasses) carry their own ``IN`` / ``OUT`` generics
    for author-facing type safety; this protocol is the runtime's structural
    contract over them.
    """

    def open(self, ctx: RuntimeContext) -> None: ...

    def process_record(self, record: StreamRecord[Any]) -> None: ...

    def process_watermark(self, watermark: Watermark) -> None: ...

    def close(self) -> None: ...

    @property
    def is_keyed(self) -> bool:
        """Whether the runtime must scope state to ``record.key`` before each
        call (true for everything downstream of a ``key_by``)."""
        ...


class BaseOperator(Generic[IN, OUT]):
    """Convenience base: stores the context, no-op watermark/close, not keyed."""

    _ctx: RuntimeContext

    def open(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    @property
    def ctx(self) -> RuntimeContext:
        return self._ctx

    @property
    def out(self) -> Collector[OUT]:
        return self._ctx.collector  # type: ignore[return-value]

    def process_record(self, record: StreamRecord[IN]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def process_watermark(self, watermark: Watermark) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def is_keyed(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Stateless transforms
# --------------------------------------------------------------------------- #
class MapOperator(BaseOperator[IN, OUT]):
    """1:1 transform of the record value, preserving timestamp and key."""

    def __init__(self, fn: Callable[[IN], OUT]) -> None:
        self._fn = fn

    def process_record(self, record: StreamRecord[IN]) -> None:
        self.out.collect(record.with_value(self._fn(record.value)))  # type: ignore[arg-type]


class FilterOperator(BaseOperator[T, T]):
    """Pass records for which the predicate is true."""

    def __init__(self, predicate: Callable[[T], bool]) -> None:
        self._predicate = predicate

    def process_record(self, record: StreamRecord[T]) -> None:
        if self._predicate(record.value):
            self.out.collect(record)


class FlatMapOperator(BaseOperator[IN, OUT]):
    """1:N transform; each output inherits the input's timestamp and key."""

    def __init__(self, fn: Callable[[IN], Iterable[OUT]]) -> None:
        self._fn = fn

    def process_record(self, record: StreamRecord[IN]) -> None:
        for value in self._fn(record.value):
            self.out.collect(record.with_value(value))  # type: ignore[arg-type]


class KeyByOperator(BaseOperator[T, T]):
    """Assign a key to each record, partitioning the stream for keyed state.

    Does not change the value; only stamps ``record.key`` so downstream keyed
    operators address per-key state and timers.
    """

    def __init__(self, key_selector: Callable[[T], object]) -> None:
        self._key_selector = key_selector

    def process_record(self, record: StreamRecord[T]) -> None:
        self.out.collect(record.with_key(self._key_selector(record.value)))


@dataclass(slots=True)
class SplitOperator(BaseOperator[T, T]):
    """Route each record to a named side output by a selector (Flink's split).

    The selector returns a tag; the record is emitted to that side output and
    also to the primary output (so a downstream union or the main lane still
    sees it). Used by the QA pipeline to fan a render event into per-stage lanes.
    """

    tag_selector: Callable[[T], str] = field(default=lambda _v: "default")

    def process_record(self, record: StreamRecord[T]) -> None:
        tag = self.tag_selector(record.value)
        self.out.collect_side(tag, record)  # type: ignore[arg-type]
        self.out.collect(record)


class ProcessFunction(BaseOperator[IN, OUT]):
    """Low-level keyed operator: full access to state, timers, side outputs.

    Subclass and override :meth:`process_element` (and optionally
    :meth:`on_timer`). This is the escape hatch the pipelines use for bespoke
    keyed logic — e.g. detecting a stall by arming a timer and clearing it when
    the next clip-ready arrives.
    """

    def process_record(self, record: StreamRecord[IN]) -> None:
        self.ctx.state.set_current_key(record.key)
        self.process_element(record, self.out)

    def process_watermark(self, watermark: Watermark) -> None:
        for key, namespace, timestamp in self.ctx.timers.advance_watermark(watermark.timestamp):
            self.ctx.state.set_current_key(key)
            self.on_timer(timestamp, namespace, self.out)

    def process_element(
        self, record: StreamRecord[IN], out: Collector[OUT]
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_timer(self, timestamp: int, namespace: str, out: Collector[OUT]) -> None:
        return None

    @property
    def is_keyed(self) -> bool:
        return True


class UnionOperator(BaseOperator[Any, Any]):
    """Merge two (or more) same-typed streams into one.

    A pass-through: every input record is forwarded unchanged to the primary
    output, preserving its timestamp and key. The runtime delivers the two
    parents' records already merged by event time, so a union is a true
    event-time interleave — the §5.6 dashboards use it to fold several render
    event sub-streams back into one lane.
    """

    def process_record(self, record: StreamRecord[Any]) -> None:
        self.out.collect(record)


class CoProcessFunction(BaseOperator[Any, OUT]):
    """Two-input keyed process function (Flink's ``CoProcessFunction``).

    The runtime tags each record by side; this operator dispatches to
    :meth:`process_left` or :meth:`process_right`, both sharing the *same* keyed
    state and timer service. This is the general two-input primitive the
    interval/table joins specialize — exposed directly for bespoke correlation
    (e.g. matching a ``render_requested`` against a ``regen_done`` while holding
    per-key in-flight state).
    """

    #: The runtime tags each input record by side before delivery.
    wants_tagged_input: bool = True

    def process_record(self, record: StreamRecord[Any]) -> None:
        self.ctx.state.set_current_key(record.key)
        tagged = record.value
        # ``tagged`` is a joins.TaggedRecord; access by attribute to avoid a cycle
        if getattr(tagged, "is_left", True):
            self.process_left(tagged.left, record, self.out)
        else:
            self.process_right(tagged.right, record, self.out)

    def process_watermark(self, watermark: Watermark) -> None:
        for key, namespace, timestamp in self.ctx.timers.advance_watermark(watermark.timestamp):
            self.ctx.state.set_current_key(key)
            self.on_timer(timestamp, namespace, self.out)

    def process_left(
        self, value: Any, record: StreamRecord[Any], out: Collector[OUT]
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def process_right(
        self, value: Any, record: StreamRecord[Any], out: Collector[OUT]
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_timer(self, timestamp: int, namespace: str, out: Collector[OUT]) -> None:
        return None

    @property
    def is_keyed(self) -> bool:
        return True


__all__ = [
    "BaseOperator",
    "CoProcessFunction",
    "Collector",
    "FilterOperator",
    "FlatMapOperator",
    "KeyByOperator",
    "MapOperator",
    "Operator",
    "ProcessFunction",
    "RuntimeContext",
    "SideOutput",
    "SplitOperator",
    "UnionOperator",
]
