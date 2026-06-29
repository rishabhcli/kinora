"""The high-level consumer — assignment, polling, offsets, group membership.

A :class:`Consumer` wraps a :class:`~app.streaming.log.broker.Broker` and gives
callers Kafka's consumer ergonomics:

* **Assignment.** Either explicit (:meth:`assign` a fixed partition set) or via a
  *group* (:meth:`subscribe` + a ``group_id``): the consumer joins the group,
  receives a partition assignment, and re-joins automatically when the broker
  reports a rebalance.
* **Position + reset.** Each assigned partition has a *position* (the next offset
  to fetch). On first read of a partition with no committed offset the
  ``auto_offset_reset`` policy (``earliest``/``latest``) picks the start; an
  out-of-range position is also reset by it.
* **Poll.** :meth:`poll` fetches a bounded batch across assigned partitions,
  advancing positions; :meth:`__aiter__` streams records continuously.
* **Commit.** :meth:`commit` (sync, explicit offsets or current positions) and
  ``enable_auto_commit`` (commit positions every ``auto_commit_interval_ms``)
  persist the group's progress. Committed offset == *next* offset to read.
* **Lag.** :meth:`lag` / :meth:`end_offsets` expose how far behind the head each
  partition is — the substrate's core health signal.

The consumer is single-task by contract (like Kafka's); share work across tasks
by running one consumer per task in the same group.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.streaming.log.broker import Broker
from app.streaming.log.errors import OffsetOutOfRangeError
from app.streaming.log.record import ConsumerRecord, TopicPartition, now_ms

__all__ = ["AutoOffsetReset", "Consumer", "ConsumerConfig"]

AutoOffsetReset = str  # "earliest" | "latest"


@dataclass(slots=True)
class ConsumerConfig:
    """Consumer tuning. Defaults: group offsets, earliest reset, manual commit."""

    group_id: str | None = None
    auto_offset_reset: AutoOffsetReset = "earliest"
    enable_auto_commit: bool = False
    auto_commit_interval_ms: int = 5000
    max_poll_records: int = 500
    assignment_protocol: str = "range"
    read_committed: bool = False


class Consumer:
    """A group-aware, offset-committing log consumer."""

    def __init__(self, broker: Broker, *, config: ConsumerConfig | None = None) -> None:
        self._broker = broker
        self._config = config or ConsumerConfig()
        self._assignment: tuple[TopicPartition, ...] = ()
        self._positions: dict[TopicPartition, int] = {}
        self._paused: set[TopicPartition] = set()
        self._subscription: tuple[str, ...] = ()
        self._member_id: str | None = None
        self._generation = -1
        self._last_auto_commit_ms = now_ms()
        self._closed = False

    # --- assignment ------------------------------------------------------ #

    async def assign(self, partitions: tuple[TopicPartition, ...]) -> None:
        """Manually assign a fixed set of partitions (no group membership)."""
        self._assignment = tuple(sorted(set(partitions)))
        await self._init_positions()

    async def subscribe(self, topics: tuple[str, ...]) -> None:
        """Subscribe to ``topics`` and join the configured group for assignment."""
        if self._config.group_id is None:
            raise ValueError("subscribe() requires a group_id; use assign() otherwise")
        self._subscription = tuple(topics)
        await self._rejoin()

    async def _rejoin(self) -> None:
        assert self._config.group_id is not None
        result = await self._broker.join_group(
            self._config.group_id,
            member_id=self._member_id,
            subscription=self._subscription,
            protocol=self._config.assignment_protocol,
        )
        self._member_id = result.member_id
        self._generation = result.generation
        self._assignment = tuple(sorted(set(result.assignment)))
        await self._init_positions()

    async def _init_positions(self) -> None:
        """Seed positions from committed offsets, else the reset policy."""
        # Drop pause flags for partitions this consumer no longer owns.
        self._paused &= set(self._assignment)
        if not self._assignment:
            self._positions = {}
            return
        committed: dict[TopicPartition, int | None] = {}
        if self._config.group_id is not None:
            committed = await self._broker.fetch_committed(
                self._config.group_id, self._assignment
            )
        beginnings = await self._broker.beginning_offsets(self._assignment)
        ends = await self._broker.end_offsets(self._assignment)
        positions: dict[TopicPartition, int] = {}
        for tp in self._assignment:
            c = committed.get(tp)
            if c is not None:
                positions[tp] = c
            elif self._config.auto_offset_reset == "latest":
                positions[tp] = ends[tp]
            else:
                positions[tp] = beginnings[tp]
        self._positions = positions

    @property
    def assignment(self) -> tuple[TopicPartition, ...]:
        """The partitions currently assigned to this consumer."""
        return self._assignment

    @property
    def member_id(self) -> str | None:
        """This consumer's group member id (``None`` for manual assignment)."""
        return self._member_id

    # --- positioning ----------------------------------------------------- #

    def position(self, tp: TopicPartition) -> int:
        """The next offset this consumer will fetch from ``tp``."""
        return self._positions[tp]

    def seek(self, tp: TopicPartition, offset: int) -> None:
        """Override the fetch position for ``tp`` (e.g. for replay)."""
        if tp not in self._positions:
            raise KeyError(f"{tp} is not assigned")
        self._positions[tp] = offset

    async def seek_to_beginning(self, *partitions: TopicPartition) -> None:
        """Seek the given (or all assigned) partitions to their earliest offset."""
        targets = partitions or self._assignment
        offsets = await self._broker.beginning_offsets(tuple(targets))
        for tp, off in offsets.items():
            self._positions[tp] = off

    async def seek_to_end(self, *partitions: TopicPartition) -> None:
        """Seek the given (or all assigned) partitions to their latest offset."""
        targets = partitions or self._assignment
        offsets = await self._broker.end_offsets(tuple(targets))
        for tp, off in offsets.items():
            self._positions[tp] = off

    async def seek_to_timestamp(self, timestamps: dict[TopicPartition, int]) -> None:
        """Seek each partition to the first offset at/after the given timestamp."""
        resolved = await self._broker.offsets_for_times(timestamps)
        ends = await self._broker.end_offsets(tuple(resolved))
        for tp, off in resolved.items():
            self._positions[tp] = off if off is not None else ends[tp]

    # --- poll ------------------------------------------------------------ #

    async def poll(
        self, *, max_records: int | None = None, max_bytes: int | None = None
    ) -> list[ConsumerRecord]:
        """Fetch a bounded batch across assigned partitions, advancing positions.

        Honours a pending rebalance by rejoining first. Records are returned in
        partition order; within a partition they're in strict offset order.
        """
        if self._closed:
            raise RuntimeError("consumer is closed")
        await self._maybe_rejoin()
        budget = max_records if max_records is not None else self._config.max_poll_records
        out: list[ConsumerRecord] = []
        for tp in self._assignment:
            if budget <= 0:
                break
            if tp in self._paused:
                continue  # flow-control: paused partitions are not fetched
            records = await self._fetch_partition(tp, budget, max_bytes)
            out.extend(records)
            budget -= len(records)
        await self._maybe_auto_commit()
        return out

    # --- flow control (pause / resume) ---------------------------------- #

    def pause(self, *partitions: TopicPartition) -> None:
        """Stop fetching the given assigned partitions until :meth:`resume`.

        Position and committed offsets are untouched — a paused partition simply
        isn't polled, the backpressure primitive the processing facet uses to
        stop reading a partition whose downstream is saturated.
        """
        for tp in partitions:
            if tp not in self._positions:
                raise KeyError(f"{tp} is not assigned")
            self._paused.add(tp)

    def resume(self, *partitions: TopicPartition) -> None:
        """Resume fetching the given partitions (the inverse of :meth:`pause`)."""
        for tp in partitions:
            self._paused.discard(tp)

    def paused(self) -> tuple[TopicPartition, ...]:
        """The currently-paused partitions (sorted, assigned-only)."""
        return tuple(sorted(self._paused & set(self._assignment)))

    async def _fetch_partition(
        self, tp: TopicPartition, budget: int, max_bytes: int | None
    ) -> list[ConsumerRecord]:
        position = self._positions[tp]
        try:
            result = await self._broker.fetch(
                tp.topic, tp.partition, position, max_records=budget, max_bytes=max_bytes
            )
        except OffsetOutOfRangeError as exc:
            self._positions[tp] = (
                exc.log_end if self._config.auto_offset_reset == "latest" else exc.log_start
            )
            result = await self._broker.fetch(
                tp.topic,
                tp.partition,
                self._positions[tp],
                max_records=budget,
                max_bytes=max_bytes,
            )
        self._positions[tp] = result.next_offset
        return list(result.records)

    async def _maybe_rejoin(self) -> None:
        if self._config.group_id is None or self._member_id is None:
            return
        alive = await self._broker.heartbeat(
            self._config.group_id, self._member_id, self._generation
        )
        if not alive:
            await self._rejoin()

    def __aiter__(self) -> AsyncIterator[ConsumerRecord]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[ConsumerRecord]:
        while not self._closed:
            batch = await self.poll()
            if not batch:
                await asyncio.sleep(0)  # yield; caller can cancel between polls
                continue
            for record in batch:
                yield record

    # --- commit ---------------------------------------------------------- #

    async def commit(self, offsets: dict[TopicPartition, int] | None = None) -> None:
        """Commit ``offsets`` (or current positions) for the group.

        Committed offset is the *next* offset to read — i.e. one past the last
        processed record, matching Kafka's commit semantics.
        """
        if self._config.group_id is None:
            raise RuntimeError("commit() requires a group_id")
        to_commit = offsets if offsets is not None else dict(self._positions)
        if not to_commit:
            return
        await self._broker.commit_offsets(
            self._config.group_id,
            to_commit,
            generation=self._generation if self._generation >= 0 else None,
            member_id=self._member_id,
        )

    async def committed(
        self, partitions: tuple[TopicPartition, ...] | None = None
    ) -> dict[TopicPartition, int | None]:
        """Read the group's committed offsets for the given (or assigned) partitions."""
        if self._config.group_id is None:
            raise RuntimeError("committed() requires a group_id")
        targets = partitions if partitions is not None else self._assignment
        return await self._broker.fetch_committed(self._config.group_id, targets)

    async def _maybe_auto_commit(self) -> None:
        if not self._config.enable_auto_commit or self._config.group_id is None:
            return
        now = now_ms()
        if (now - self._last_auto_commit_ms) >= self._config.auto_commit_interval_ms:
            await self.commit()
            self._last_auto_commit_ms = now

    # --- lag ------------------------------------------------------------- #

    async def end_offsets(
        self, partitions: tuple[TopicPartition, ...] | None = None
    ) -> dict[TopicPartition, int]:
        """The high watermark (next offset) per partition."""
        targets = partitions if partitions is not None else self._assignment
        return await self._broker.end_offsets(targets)

    async def lag(self) -> dict[TopicPartition, int]:
        """Records behind the head per assigned partition (``end - position``)."""
        ends = await self._broker.end_offsets(self._assignment)
        return {tp: max(0, ends[tp] - self._positions[tp]) for tp in self._assignment}

    # --- lifecycle ------------------------------------------------------- #

    async def close(self) -> None:
        """Auto-commit (if enabled), leave the group, and mark closed."""
        if self._closed:
            return
        if self._config.enable_auto_commit and self._config.group_id is not None:
            with contextlib.suppress(Exception):
                # A commit race at close (revoked generation) must not raise.
                await self.commit()
        if self._config.group_id is not None and self._member_id is not None:
            await self._broker.leave_group(self._config.group_id, self._member_id)
        self._closed = True
