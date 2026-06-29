"""The CDC pipeline — wires a source through dedup + checkpointing to a sink.

This is the orchestration seam: given a :class:`CDCSource`, a
:class:`ChangeSink` (the view engine, a broker adapter, or a fan-out of both),
and an :class:`OffsetStore`, :class:`CDCPipeline` runs the snapshot+stream
bootstrap, applies schema migration to each row, de-duplicates replays, hands
each event to the sink, and commits offsets so a restart resumes exactly.

Guarantees
----------
* **At-least-once delivery** to the sink (the standard CDC contract): a crash
  between sink-emit and offset-commit replays a bounded tail, never drops.
* **Idempotent application** is the sink's responsibility; the view engine is
  idempotent under replay because re-applying the same retract/assert delta for
  an already-current key is a no-op (the projected-row bookkeeping absorbs it).
* **Monotonic offsets**: the pipeline never commits a position below the last
  committed one (the offset store enforces this too).

The pipeline holds no timers: :meth:`run` consumes the source's async iterator
to exhaustion. A live deployment wraps a long-lived source whose iterator blocks
for new WAL; the deterministic tests use the fake stream, which ends, so
:meth:`run` returns a :class:`PipelineResult` for assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.streaming.cdc.events import ChangeEvent, LogPosition, Op
from app.streaming.cdc.offsets import InMemoryOffsetStore, OffsetStore
from app.streaming.cdc.schema import SchemaRegistry
from app.streaming.cdc.sink import ChangeSink
from app.streaming.cdc.snapshot import SnapshotCoordinator, SnapshotState
from app.streaming.cdc.source import CDCSource


@dataclass(slots=True)
class PipelineResult:
    """Summary of a :meth:`CDCPipeline.run` (metrics + final position)."""

    delivered: int = 0
    deduped: int = 0
    schema_events: int = 0
    snapshot_rows: int = 0
    last_position: LogPosition = LogPosition.zero()
    committed_position: LogPosition = LogPosition.zero()
    per_table: dict[str, int] = field(default_factory=dict)


class CDCPipeline:
    """Drives one connector: source → migrate → dedup → sink → checkpoint."""

    def __init__(
        self,
        *,
        connector: str,
        source: CDCSource,
        sink: ChangeSink,
        offsets: OffsetStore | None = None,
        schema_registry: SchemaRegistry | None = None,
        commit_every: int = 1,
        resume: bool = True,
    ) -> None:
        self.connector = connector
        self._source = source
        self._sink = sink
        self._offsets = offsets or InMemoryOffsetStore()
        self._schema = schema_registry or SchemaRegistry()
        self._commit_every = max(1, commit_every)
        self._resume = resume
        # Last position delivered per table, for dedup of at-least-once replays.
        self._seen: dict[str, LogPosition] = {}

    @property
    def schema_registry(self) -> SchemaRegistry:
        return self._schema

    async def run(self) -> PipelineResult:
        """Run the full bootstrap + stream to source exhaustion."""
        result = PipelineResult()

        resume_from: LogPosition | None = None
        if self._resume:
            resume_from = await self._offsets.load(self.connector, "__all__")
            if resume_from == LogPosition.zero():
                resume_from = None

        coordinator = SnapshotCoordinator(self._source, resume_from=resume_from)
        since_commit = 0
        async for event in coordinator.run():
            handled = await self._handle(event, result)
            if handled:
                since_commit += 1
                if since_commit >= self._commit_every:
                    await self._commit(event.position)
                    result.committed_position = event.position
                    since_commit = 0

        # Final checkpoint at the last position seen.
        if result.last_position > result.committed_position:
            await self._commit(result.last_position)
            result.committed_position = result.last_position
        result.snapshot_rows = coordinator.progress.rows_snapshotted
        return result

    async def _handle(self, event: ChangeEvent, result: PipelineResult) -> bool:
        """Process one event; return whether it counted as a delivered change."""
        result.last_position = max(result.last_position, event.position)

        if event.op is Op.SCHEMA:
            self._apply_schema(event)
            result.schema_events += 1
            await self._sink.emit(event)
            return True

        if event.op is Op.HEARTBEAT:
            # Heartbeats advance the offset without a delivery; forward so a
            # downstream that tracks liveness sees it, but don't count it.
            await self._sink.emit(event)
            return True

        # Dedup: an at-least-once replay re-presents an event we already
        # delivered for this table at a <= position.
        last = self._seen.get(event.table, LogPosition.zero())
        if not event.is_snapshot and event.position <= last:
            result.deduped += 1
            return False

        migrated = self._migrate(event)
        await self._sink.emit(migrated)
        if not event.is_snapshot:
            self._seen[event.table] = event.position
        result.delivered += 1
        result.per_table[event.table] = result.per_table.get(event.table, 0) + 1
        return True

    def _apply_schema(self, event: ChangeEvent) -> None:
        from app.streaming.cdc.schema import TableSchema

        columns = event.after or {}
        renames = (event.meta or {}).get("renames")
        schema = TableSchema.from_mapping(
            event.table,
            event.schema_version,
            {k: (v if isinstance(v, str) else "any") for k, v in columns.items()},
        )
        self._schema.register(schema, renames=renames)

    def _migrate(self, event: ChangeEvent) -> ChangeEvent:
        """Bring the event's row image up to the latest registered schema."""
        latest = self._schema.latest(event.table)
        if latest is None or event.schema_version >= latest.version:
            return event
        from dataclasses import replace

        new_after = (
            self._schema.migrate(event.after, event.table, event.schema_version)
            if event.after is not None
            else None
        )
        new_before = (
            self._schema.migrate(event.before, event.table, event.schema_version)
            if event.before is not None
            else None
        )
        return replace(event, after=new_after, before=new_before, schema_version=latest.version)

    async def _commit(self, position: LogPosition) -> None:
        await self._offsets.commit(self.connector, "__all__", position)

    @property
    def committed(self) -> LogPosition:
        return self._seen.get("__all__", LogPosition.zero())

    @property
    def snapshot_state(self) -> SnapshotState:  # pragma: no cover - convenience
        return SnapshotState.DONE


__all__ = ["CDCPipeline", "PipelineResult"]
