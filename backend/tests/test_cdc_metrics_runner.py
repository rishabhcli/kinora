"""Tests for CDC metrics + the high-level runner (no infra)."""

from __future__ import annotations

from app.streaming.cdc.clock import FakeClock
from app.streaming.cdc.events import ChangeEvent, LogPosition
from app.streaming.cdc.metrics import CdcMetrics, MeteredSink
from app.streaming.cdc.runner import CDCRunner, build_kinora_views
from app.streaming.cdc.sink import InMemorySink
from app.streaming.cdc.source import FakeChangeStream


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_metrics_counts_per_table_and_op() -> None:
    m = CdcMetrics()
    m.record_event(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0)))
    m.record_event(ChangeEvent.update("books", None, {"id": "b1"}, LogPosition(2, 0)))
    m.record_event(ChangeEvent.delete("books", {"id": "b1"}, LogPosition(3, 0)))
    m.record_event(ChangeEvent.read("pages", {"id": "pg1"}, LogPosition(0, 0)))
    m.record_event(ChangeEvent.heartbeat(LogPosition(4, 0)))
    snap = m.snapshot()
    assert snap["tables"]["books"]["inserts"] == 1
    assert snap["tables"]["books"]["updates"] == 1
    assert snap["tables"]["books"]["deletes"] == 1
    assert snap["tables"]["pages"]["reads"] == 1
    assert snap["heartbeats"] == 1
    assert snap["total_events"] == 4


def test_metrics_lag_is_deterministic_with_fake_clock() -> None:
    clk = FakeClock(start=1000.0)
    m = CdcMetrics(clock=clk)
    # An event observed 5s ago at the source.
    m.record_event(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0), ts=995.0))
    snap = m.snapshot()
    assert snap["last_lag_s"] == 5.0
    assert snap["max_lag_s"] == 5.0
    # A fresher event reduces last_lag but max_lag is sticky.
    m.record_event(ChangeEvent.insert("books", {"id": "b2"}, LogPosition(2, 0), ts=1000.0))
    snap = m.snapshot()
    assert snap["last_lag_s"] == 0.0
    assert snap["max_lag_s"] == 5.0


def test_metrics_view_lag() -> None:
    m = CdcMetrics()
    m.set_source_head(LogPosition(100, 0))
    assert m.view_lag(LogPosition(90, 0)) == 10
    assert m.view_lag(LogPosition(100, 0)) == 0
    assert m.view_lag(LogPosition(120, 0)) == 0  # never negative


async def test_metered_sink_records_and_forwards() -> None:
    inner = InMemorySink()
    m = CdcMetrics()
    sink = MeteredSink(inner, m)
    await sink.emit(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(7, 0)))
    assert len(inner.events) == 1  # forwarded
    assert m.snapshot()["total_events"] == 1
    assert m.snapshot()["source_head_major"] == 7


async def test_metered_sink_records_errors() -> None:
    class _Boom:
        async def emit(self, event: ChangeEvent) -> None:
            raise RuntimeError("boom")

    m = CdcMetrics()
    sink = MeteredSink(_Boom(), m)
    raised = False
    try:
        await sink.emit(ChangeEvent.insert("books", {"id": "b1"}, LogPosition(1, 0)))
    except RuntimeError:
        raised = True
    assert raised
    assert m.snapshot()["errors"] == 1


# --------------------------------------------------------------------------- #
# Runner + canonical Kinora view set
# --------------------------------------------------------------------------- #
def test_build_kinora_views_registers_expected_set() -> None:
    engine = build_kinora_views()
    assert engine.graph.views == {
        "library_shelf",
        "canon_graph",
        "shots_per_book",
        "accepted_shots_per_book",
        "characters_per_book",
    }


async def test_runner_drives_full_projection_set() -> None:
    src = FakeChangeStream()
    src.seed_snapshot(
        "books",
        [{"id": "b1", "title": "Dune", "status": "ready"}],
    )
    src.push_insert("shots", {"id": "s1", "book_id": "b1", "status": "accepted"})
    src.push_insert("shots", {"id": "s2", "book_id": "b1", "status": "planned"})
    src.push_insert(
        "entities",
        {
            "id": "e1",
            "book_id": "b1",
            "entity_key": "char_paul",
            "type": "character",
            "name": "Paul",
            "version": 1,
            "valid_to_beat": None,
        },
        key_columns=("book_id", "entity_key"),
    )

    runner = CDCRunner(connector="kinora", source=src)
    result = await runner.run()

    # Library shelf reflects the snapshot.
    assert [r["book_id"] for r in runner.rows("library_shelf")] == ["b1"]
    # Aggregates reflect the stream.
    assert runner.rows("shots_per_book")[0]["shot_count"] == 2
    assert runner.rows("accepted_shots_per_book")[0]["accepted"] == 1
    assert runner.rows("characters_per_book")[0]["characters"] == 1
    # Metrics recorded the full flow.
    assert result.metrics["total_events"] >= 4
    assert result.pipeline.delivered >= 4


async def test_runner_with_extra_broker_sink() -> None:
    class _Broker:
        def __init__(self) -> None:
            self.published: list[tuple[str, object]] = []

        async def publish(self, topic: str, payload: object) -> int:
            self.published.append((topic, payload))
            return 1

    from app.streaming.cdc.sink import BrokerSink

    broker = _Broker()
    src = FakeChangeStream()
    src.push_insert("books", {"id": "b1", "title": "A", "status": "ready"})
    runner = CDCRunner(connector="k", source=src, extra_sinks=[BrokerSink(broker)])
    await runner.run()
    assert any(t == "cdc.books" for t, _ in broker.published)
    assert len(runner.rows("library_shelf")) == 1
