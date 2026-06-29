"""Joins: stream-stream interval joins and stream-table enrichment joins.

Two join shapes cover Kinora's needs:

* **Interval join** (:class:`IntervalJoinOperator`) — joins two keyed streams
  where a record from the left matches a record from the right whose event time
  falls in ``[left.ts + lower, left.ts + upper]``. Each side buffers its records
  in keyed state and evicts them once the watermark guarantees no future match.
  This is the right tool for correlating a *render request* with its later
  *clip-ready* (latency) or a *reader-intent* with the *clip that served it*.

* **Stream-table join** (:class:`StreamTableJoinOperator`) — enriches a stream
  with the latest value from a slowly-changing *table* (a keyed, last-write-wins
  view built from a second stream). This is the classic dimension-table
  enrichment: tag every reader-intent with the book's current canon version, or
  every render event with the session that requested it.

Both keep a bounded amount of state and clean it up on watermark progress, so
they run forever without leaking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.streaming.processing.operators import BaseOperator
from app.streaming.processing.records import StreamRecord, Watermark
from app.streaming.processing.state import (
    ListStateDescriptor,
    ValueStateDescriptor,
)

L = TypeVar("L")
R = TypeVar("R")
OUT = TypeVar("OUT")
TBL = TypeVar("TBL")
S = TypeVar("S")


@dataclass(frozen=True, slots=True)
class TaggedRecord(Generic[L, R]):
    """A union-typed element on the merged input of a two-input operator.

    The runtime tags each record with which logical input (``LEFT`` / ``RIGHT``)
    it came from when two streams are connected into one operator.
    """

    is_left: bool
    left: L | None = None
    right: R | None = None


# --------------------------------------------------------------------------- #
# Stream-stream interval join
# --------------------------------------------------------------------------- #
class IntervalJoinOperator(
    BaseOperator[TaggedRecord[L, R], OUT], Generic[L, R, OUT]
):
    """Keyed interval join of two streams.

    A left record at ``t`` joins every right record at ``t'`` with
    ``t + lower_ms <= t' <= t + upper_ms`` (and vice versa, symmetrically). Both
    sides are buffered per key; the watermark drives eviction:

    * a left record can be evicted once ``watermark > left.ts + upper`` (no
      future right record can still fall in its window),
    * a right record once ``watermark > right.ts - lower``.

    ``join_fn`` builds the output value from a matched ``(left, right)`` pair.
    """

    def __init__(
        self,
        *,
        lower_ms: int,
        upper_ms: int,
        join_fn: Callable[[L, R], OUT],
    ) -> None:
        if lower_ms > upper_ms:
            raise ValueError("interval join lower bound must be <= upper bound")
        self._lower = lower_ms
        self._upper = upper_ms
        self._join = join_fn
        # buffers hold (timestamp, value) pairs per key
        self._left_buf: ListStateDescriptor[tuple[int, L]] = ListStateDescriptor("ij-left")
        self._right_buf: ListStateDescriptor[tuple[int, R]] = ListStateDescriptor("ij-right")

    #: The runtime tags each input record by side before delivery.
    wants_tagged_input: bool = True

    @property
    def is_keyed(self) -> bool:
        return True

    def process_record(self, record: StreamRecord[TaggedRecord[L, R]]) -> None:
        ctx = self.ctx
        ctx.state.set_current_key(record.key)
        tagged = record.value
        ts = record.timestamp
        left_state = ctx.state.get_list_state(self._left_buf)
        right_state = ctx.state.get_list_state(self._right_buf)

        if tagged.is_left:
            assert tagged.left is not None
            left_state.add((ts, tagged.left))
            # match against buffered right records within [ts+lower, ts+upper]
            for r_ts, r_val in right_state.get():
                if ts + self._lower <= r_ts <= ts + self._upper:
                    self._emit(record.key, ts, r_ts, tagged.left, r_val)
        else:
            assert tagged.right is not None
            right_state.add((ts, tagged.right))
            # a right record at r_ts matches a left at l_ts when
            # l_ts + lower <= r_ts <= l_ts + upper  =>  r_ts-upper <= l_ts <= r_ts-lower
            for l_ts, l_val in left_state.get():
                if l_ts + self._lower <= ts <= l_ts + self._upper:
                    self._emit(record.key, l_ts, ts, l_val, tagged.right)

    def _emit(self, key: object, l_ts: int, r_ts: int, left: L, right: R) -> None:
        # Output carries the later of the two timestamps (when the join completed).
        self.out.collect_value(self._join(left, right), timestamp=max(l_ts, r_ts), key=key)

    def process_watermark(self, watermark: Watermark) -> None:
        ctx = self.ctx
        wm = watermark.timestamp
        # Evict expired buffer entries for every key that has join state. A left
        # record can be evicted once no future right record could fall in its
        # window (``wm > ts + upper``); a right record once ``wm > ts - lower``.
        for key in ctx.state.keys_with_state("ij-left"):
            ctx.state.set_current_key(key)
            left_state = ctx.state.get_list_state(self._left_buf)
            left_state.update([(t, v) for t, v in left_state.get() if t + self._upper >= wm])
        for key in ctx.state.keys_with_state("ij-right"):
            ctx.state.set_current_key(key)
            right_state = ctx.state.get_list_state(self._right_buf)
            right_state.update([(t, v) for t, v in right_state.get() if t - self._lower >= wm])


# --------------------------------------------------------------------------- #
# Stream-table (enrichment) join
# --------------------------------------------------------------------------- #
class TableSink(Generic[TBL]):
    """Builds a last-write-wins keyed table from a stream of updates.

    Each input record updates the value for its key. The companion
    :class:`StreamTableJoinOperator` reads this table to enrich a main stream.
    Used as a small co-process operator; in the single-input runtime the table
    is materialized first (bounded, broadcast-style) and passed in.
    """

    def __init__(self, key_selector: Callable[[TBL], object]) -> None:
        self._key_selector = key_selector
        self._table: dict[object, TBL] = {}

    def update(self, value: TBL) -> None:
        self._table[self._key_selector(value)] = value

    def get(self, key: object) -> TBL | None:
        return self._table.get(key)

    def snapshot(self) -> dict[object, TBL]:
        return dict(self._table)


class StreamTableJoinOperator(BaseOperator[S, OUT], Generic[S, TBL, OUT]):
    """Enrich each stream record with the current table value for its key.

    The ``table`` is a keyed, slowly-changing dimension (last-write-wins). When a
    stream record's key has a table entry, ``join_fn(stream, table)`` produces
    the enriched output; when it has none, ``join_fn(stream, None)`` is called so
    the pipeline decides whether to emit an un-enriched record or drop it.

    Updates to the table arrive through :meth:`update_table` (the runtime routes
    the dimension stream there); the join uses the latest value as of the moment
    the stream record is processed — event-time-correct for a slowly-changing
    dimension whose updates lead the fact stream.
    """

    def __init__(
        self,
        *,
        join_fn: Callable[[S, TBL | None], OUT | None],
        table_key_selector: Callable[[TBL], object],
    ) -> None:
        self._join = join_fn
        self._table_key = table_key_selector
        # the materialized table lives in keyed value-state under a single
        # broadcast key so it checkpoints with everything else.
        self._cell: ValueStateDescriptor[dict[object, TBL]] = ValueStateDescriptor(
            "stj-table", default=None
        )

    @property
    def is_keyed(self) -> bool:
        return True

    def update_table(self, value: TBL) -> None:
        self.ctx.state.set_current_key("__table__")
        cell = self.ctx.state.get_value_state(self._cell)
        table = cell.value() or {}
        table[self._table_key(value)] = value
        cell.update(table)

    def process_record(self, record: StreamRecord[S]) -> None:
        self.ctx.state.set_current_key("__table__")
        table = self.ctx.state.get_value_state(self._cell).value() or {}
        enriched = self._join(record.value, table.get(record.key))
        if enriched is not None:
            self.out.collect_value(enriched, timestamp=record.timestamp, key=record.key)
