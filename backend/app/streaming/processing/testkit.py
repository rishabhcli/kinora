"""Deterministic event-time test driver.

Testing a streaming operator means controlling time. This harness lets a test
*push records and watermarks in an exact order* and inspect what comes out — no
wall-clock, no sleeps, no flakiness. It is the operator-level analogue of Flink's
``OneInputStreamOperatorTestHarness``.

Two entry points:

* :class:`TestHarness` — drive a single operator: ``process_record`` /
  ``process_watermark`` / ``process_value`` push input; ``output`` and
  ``side_output`` read results; the keyed-state backend and timer service are
  real, so state and lateness behave exactly as in production.
* :func:`collect` — run a whole :class:`StreamEnvironment` and pull a node's
  values in one call, for pipeline-level assertions.

Records are stamped with explicit event-time; the harness never invents a
timestamp, so every test reads as a precise event-time timeline.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.operators import Collector, Operator, RuntimeContext
from app.streaming.processing.records import StreamRecord, Watermark
from app.streaming.processing.state import KeyedStateBackend
from app.streaming.processing.time_domain import EventTimeTimerService

IN = TypeVar("IN")
OUT = TypeVar("OUT")


class TestHarness(Generic[IN, OUT]):
    """Drives one operator with explicit event-time control."""

    # Not a pytest test class despite the name — it is the test *driver*.
    __test__ = False

    def __init__(self, operator: Operator, *, operator_id: str = "test-op") -> None:
        self.operator = operator
        self.backend = KeyedStateBackend(operator_id)
        self.timers = EventTimeTimerService()
        self.collector: Collector[object] = Collector()
        self.ctx = RuntimeContext(
            operator_id=operator_id,
            state=self.backend,
            timers=self.timers,
            collector=self.collector,
            current_watermark=0,
        )
        operator.open(self.ctx)
        self._current_watermark = 0

    def process_record(self, record: StreamRecord[IN]) -> TestHarness[IN, OUT]:
        self.ctx.current_watermark = self._current_watermark
        self.operator.process_record(record)
        return self

    def process_value(
        self, value: IN, timestamp: int, *, key: object | None = None
    ) -> TestHarness[IN, OUT]:
        return self.process_record(StreamRecord(value=value, timestamp=timestamp, key=key))

    def process_watermark(self, timestamp: int) -> TestHarness[IN, OUT]:
        self._current_watermark = timestamp
        self.ctx.current_watermark = timestamp
        self.operator.process_watermark(Watermark(timestamp))
        return self

    def close(self) -> TestHarness[IN, OUT]:
        self.operator.close()
        return self

    @property
    def output(self) -> list[StreamRecord[OUT]]:
        return list(self.collector.primary)  # type: ignore[arg-type]

    def output_values(self) -> list[OUT]:
        return [r.value for r in self.collector.primary]  # type: ignore[misc]

    def side_output(self, tag: str) -> list[StreamRecord[object]]:
        return list(self.collector.side_outputs.get(tag, []))

    def pending_timers(self) -> int:
        return self.timers.pending()

    def take_snapshot(self, checkpoint_id: int = 1) -> object:
        return self.backend.snapshot(checkpoint_id)


def collect(env: StreamEnvironment, node_id: str, *, name: str = "test-job") -> list[object]:
    """Execute ``env`` and return the values emitted by ``node_id``."""

    result = env.execute(name=name)
    return result.values(node_id)
