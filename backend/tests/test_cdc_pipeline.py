"""End-to-end CDC pipeline tests over the deterministic fake stream (no infra)."""

from __future__ import annotations

from typing import Any

import pytest

from app.streaming.cdc.events import ChangeEvent, LogPosition, Op
from app.streaming.cdc.offsets import InMemoryOffsetStore
from app.streaming.cdc.pipeline import CDCPipeline
from app.streaming.cdc.schema import SchemaRegistry, TableSchema
from app.streaming.cdc.sink import (
    BrokerSink,
    FanoutSink,
    InMemorySink,
    NullSink,
    RedisStreamSink,
)
from app.streaming.cdc.snapshot import SnapshotCoordinator, SnapshotState
from app.streaming.cdc.source import FakeChangeStream
from app.streaming.cdc.views import LibraryShelfView, MaterializedViewEngine


# --------------------------------------------------------------------------- #
# Snapshot + stream bootstrap
# --------------------------------------------------------------------------- #
async def test_snapshot_then_stream_no_loss_no_dup() -> None:
    src = FakeChangeStream()
    src.seed_snapshot("books", [{"id": "b1", "title": "A", "status": "ready"}])
    # A change that lands "during" the snapshot is still streamed exactly once.
    src.push_insert("books", {"id": "b2", "title": "B", "status": "ready"})

    coordinator = SnapshotCoordinator(src)
    events = [e async for e in coordinator.run()]
    ops = [e.op for e in events]
    assert ops == [Op.READ, Op.INSERT]
    assert coordinator.state is SnapshotState.DONE
    assert coordinator.progress.rows_snapshotted == 1
    assert coordinator.progress.stream_events == 1


async def test_pipeline_bootstraps_into_view_engine() -> None:
    src = FakeChangeStream()
    src.seed_snapshot(
        "books",
        [
            {"id": "b1", "title": "A", "status": "ready"},
            {"id": "b2", "title": "B", "status": "importing"},
        ],
    )
    src.push_update("books", None, {"id": "b2", "title": "B", "status": "ready"})
    src.push_insert("books", {"id": "b3", "title": "C", "status": "ready"})

    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    pipe = CDCPipeline(connector="lib", source=src, sink=engine)
    result = await pipe.run()

    assert result.snapshot_rows == 2
    rows = {r["book_id"]: r for r in engine.rows("library_shelf")}
    assert set(rows) == {"b1", "b2", "b3"}
    assert rows["b2"]["status"] == "ready"

    # IVM consistency: the incremental state equals a from-scratch recompute.
    live = [
        {"id": "b1", "title": "A", "status": "ready"},
        {"id": "b2", "title": "B", "status": "ready"},
        {"id": "b3", "title": "C", "status": "ready"},
    ]
    assert engine.verify({"books": live})["library_shelf"].consistent


# --------------------------------------------------------------------------- #
# Offsets + resume
# --------------------------------------------------------------------------- #
async def test_pipeline_commits_and_resumes() -> None:
    offsets = InMemoryOffsetStore()
    src = FakeChangeStream()
    src.push_insert("books", {"id": "b1", "title": "A", "status": "ready"})
    src.push_insert("books", {"id": "b2", "title": "B", "status": "ready"})

    sink1 = InMemorySink()
    pipe1 = CDCPipeline(connector="lib", source=src, sink=sink1, offsets=offsets)
    r1 = await pipe1.run()
    committed = await offsets.load("lib", "__all__")
    assert committed == r1.last_position
    assert committed > LogPosition.zero()

    # A new pipeline on the same source + offsets resumes after the commit:
    # nothing new to deliver.
    sink2 = InMemorySink()
    pipe2 = CDCPipeline(connector="lib", source=src, sink=sink2, offsets=offsets)
    r2 = await pipe2.run()
    assert r2.delivered == 0
    assert len(sink2.events) == 0


async def test_pipeline_dedups_replayed_events() -> None:
    # A source that re-serves the same events (at-least-once replay).
    src = FakeChangeStream()
    e1 = src.push_insert("books", {"id": "b1", "status": "ready"})
    src.push(e1)  # duplicate at the same position
    sink = InMemorySink()
    pipe = CDCPipeline(connector="lib", source=src, sink=sink)
    result = await pipe.run()
    assert result.delivered == 1
    assert result.deduped == 1


# --------------------------------------------------------------------------- #
# Schema migration in-flight
# --------------------------------------------------------------------------- #
async def test_pipeline_migrates_old_rows_via_schema_event() -> None:
    reg = SchemaRegistry()
    reg.register(TableSchema.from_mapping("books", 1, {"id": "str", "title": "str"}))

    src = FakeChangeStream()
    # An old (v1) row...
    src.push_insert("books", {"id": "b1", "title": "Dune"}, schema_version=1)
    # ...then a schema bump to v2 adding `status` with a default...
    src.push(
        ChangeEvent.schema(
            "books",
            {"id": "str", "title": "str", "status": "str"},
            LogPosition(50, 0),
            schema_version=2,
        )
    )
    sink = InMemorySink()
    pipe = CDCPipeline(connector="lib", source=src, sink=sink, schema_registry=reg)
    # Register the v2 schema with the added column default through the event.
    await pipe.run()
    latest = pipe.schema_registry.latest("books")
    assert latest is not None and latest.version == 2

    # A *new* v1 row arriving after the bump gets migrated up to v2.
    src.push_insert("books", {"id": "b2", "title": "Messiah"}, schema_version=1)
    sink.clear()
    await CDCPipeline(
        connector="lib2", source=src, sink=sink, schema_registry=reg, resume=False
    ).run()
    b2 = next(
        e
        for e in sink.events
        if e.is_row_event and (e.after or {}).get("id") == "b2"
    )
    assert b2.schema_version == 2
    assert b2.after is not None and "status" in b2.after  # back-filled


# --------------------------------------------------------------------------- #
# Sinks
# --------------------------------------------------------------------------- #
class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any) -> int:
        self.published.append((topic, payload))
        return 1


async def test_broker_sink_publishes_per_table_topic() -> None:
    broker = _FakeBroker()
    sink = BrokerSink(broker, prefix="cdc")
    await sink.emit(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0)))
    await sink.emit(ChangeEvent.heartbeat(LogPosition(2, 0)))
    topics = [t for t, _ in broker.published]
    assert topics == ["cdc.books", "cdc.__heartbeat__"]


async def test_redis_stream_sink_publishes() -> None:
    broker = _FakeBroker()  # duck-typed: has publish(channel, message)
    sink = RedisStreamSink(broker, prefix="cdc")
    await sink.emit(ChangeEvent.insert("entities", {"id": "e1"}, LogPosition(1, 0)))
    assert broker.published[0][0] == "cdc.entities"


async def test_fanout_drives_engine_and_broker() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    broker = _FakeBroker()
    fan = FanoutSink([engine, BrokerSink(broker)])

    src = FakeChangeStream()
    src.push_insert("books", {"id": "b1", "title": "A", "status": "ready"})
    pipe = CDCPipeline(connector="lib", source=src, sink=fan)
    await pipe.run()

    assert len(engine.rows("library_shelf")) == 1
    assert broker.published[0][0] == "cdc.books"


async def test_fanout_isolates_child_failures() -> None:
    class _Boom:
        async def emit(self, event: ChangeEvent) -> None:
            raise RuntimeError("boom")

    good = InMemorySink()
    fan = FanoutSink([_Boom(), good])
    with pytest.raises(RuntimeError):
        await fan.emit(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0)))
    # The good sink still received the event despite the sibling raising.
    assert len(good.events) == 1


async def test_null_sink_discards() -> None:
    sink = NullSink()
    await sink.emit(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0)))  # no raise
