"""Unit tests for the CDC sources: fake stream, WAL decode, polling (no infra)."""

from __future__ import annotations

from app.streaming.cdc.clock import FakeClock
from app.streaming.cdc.events import LogPosition, Op
from app.streaming.cdc.polling_source import ListRowFetcher, PollingSource
from app.streaming.cdc.source import FakeChangeStream, ReplayBuffer
from app.streaming.cdc.wal_source import (
    ListWalReader,
    PostgresLogicalSource,
    decode_wal2json,
    parse_lsn,
)


async def _drain(aiter):  # type: ignore[no-untyped-def]
    return [e async for e in aiter]


# --------------------------------------------------------------------------- #
# FakeChangeStream
# --------------------------------------------------------------------------- #
async def test_fake_stream_orders_and_resumes() -> None:
    src = FakeChangeStream(start_lsn=1)
    src.push_insert("books", {"id": "b1", "title": "A"})
    src.push_update("books", {"id": "b1", "title": "A"}, {"id": "b1", "title": "B"})
    src.push_delete("books", {"id": "b1", "title": "B"})

    events = await _drain(src.stream())
    assert [e.op for e in events] == [Op.INSERT, Op.UPDATE, Op.DELETE]
    positions = [e.position for e in events]
    assert positions == sorted(positions)

    # Resume after the first event yields only the tail.
    tail = await _drain(src.stream(after=events[0].position))
    assert [e.op for e in tail] == [Op.UPDATE, Op.DELETE]


async def test_fake_stream_snapshot_orders_before_stream() -> None:
    src = FakeChangeStream()
    src.seed_snapshot("books", [{"id": "b1"}, {"id": "b2"}])
    src.push_insert("books", {"id": "b3"})
    snap = await _drain(src.snapshot())
    assert all(e.is_snapshot for e in snap)
    assert all(e.position.major == 0 for e in snap)
    stream = await _drain(src.stream())
    assert stream[0].position.major >= 1


# --------------------------------------------------------------------------- #
# WAL decode
# --------------------------------------------------------------------------- #
def test_parse_lsn() -> None:
    assert parse_lsn("0/16B3748") == (0 << 32) | 0x16B3748
    assert parse_lsn("1/0") == (1 << 32)
    assert parse_lsn(42) == 42


def test_decode_wal2json_all_kinds() -> None:
    record = {
        "lsn": "0/100",
        "change": [
            {
                "kind": "insert",
                "table": "books",
                "columnnames": ["id", "title"],
                "columnvalues": ["b1", "Dune"],
            },
            {
                "kind": "update",
                "table": "books",
                "columnnames": ["id", "title"],
                "columnvalues": ["b1", "Dune2"],
                "oldkeys": {"keynames": ["id"], "keyvalues": ["b1"]},
            },
            {
                "kind": "delete",
                "table": "books",
                "oldkeys": {"keynames": ["id"], "keyvalues": ["b1"]},
            },
        ],
    }
    events = decode_wal2json(record)
    assert [e.op for e in events] == [Op.INSERT, Op.UPDATE, Op.DELETE]
    # Per-change index becomes minor, so all share the txn LSN and stay ordered.
    assert {e.position.major for e in events} == {parse_lsn("0/100")}
    assert [e.position.minor for e in events] == [0, 1, 2]
    assert events[0].after == {"id": "b1", "title": "Dune"}
    assert events[1].before == {"id": "b1"}
    assert events[2].key == {"id": "b1"}


async def test_postgres_logical_source_streams_after_cutoff() -> None:
    reader = ListWalReader(
        [
            {
                "lsn": "0/10",
                "change": [
                    {
                        "kind": "insert",
                        "table": "books",
                        "columnnames": ["id"],
                        "columnvalues": ["b1"],
                    }
                ],
            },
            {
                "lsn": "0/20",
                "change": [
                    {
                        "kind": "insert",
                        "table": "books",
                        "columnnames": ["id"],
                        "columnvalues": ["b2"],
                    }
                ],
            },
        ]
    )
    src = PostgresLogicalSource(reader)
    events = await _drain(src.stream())
    assert [e.after["id"] for e in events] == ["b1", "b2"]
    assert src.head_position == LogPosition(parse_lsn("0/20"), 0)

    tail = await _drain(src.stream(after=LogPosition(parse_lsn("0/10"), 0)))
    assert [e.after["id"] for e in tail] == ["b2"]


# --------------------------------------------------------------------------- #
# Polling source + tombstone strategy
# --------------------------------------------------------------------------- #
async def test_polling_compound_cursor_spans_batch_boundary() -> None:
    # Regression: rows sharing one updated_at must not be skipped when the batch
    # boundary splits them (the compound (updated_at, pk) cursor fixes this).
    fetcher = ListRowFetcher()
    fetcher.set_rows(
        "books",
        [
            {"id": "a", "__updated_at_micros": 100, "deleted_at": None},
            {"id": "b", "__updated_at_micros": 100, "deleted_at": None},
            {"id": "c", "__updated_at_micros": 100, "deleted_at": None},
        ],
    )
    src = PollingSource(fetcher, ["books"], batch_size=2)
    events = await src.poll_once()
    assert sorted(e.key["id"] for e in events) == ["a", "b", "c"]


async def test_polling_emits_insert_then_update() -> None:
    fetcher = ListRowFetcher()
    clk = FakeClock()
    src = PollingSource(fetcher, ["books"], clock=clk)

    fetcher.set_rows(
        "books", [{"id": "b1", "title": "A", "__updated_at_micros": 100, "deleted_at": None}]
    )
    first = await src.poll_once()
    assert [e.op for e in first] == [Op.INSERT]

    # Same row touched again (higher updated_at) → UPDATE.
    fetcher.upsert(
        "books", {"id": "b1", "title": "B", "__updated_at_micros": 200, "deleted_at": None}
    )
    second = await src.poll_once()
    assert [e.op for e in second] == [Op.UPDATE]
    assert second[0].after is not None
    assert second[0].after["title"] == "B"


async def test_polling_tombstone_emits_delete() -> None:
    fetcher = ListRowFetcher()
    src = PollingSource(fetcher, ["books"])
    fetcher.set_rows(
        "books", [{"id": "b1", "title": "A", "__updated_at_micros": 100, "deleted_at": None}]
    )
    await src.poll_once()
    # Soft-delete (deleted_at set + bumped updated_at) → a DELETE tombstone.
    fetcher.upsert(
        "books", {"id": "b1", "title": "A", "__updated_at_micros": 300, "deleted_at": "2026-01-01"}
    )
    events = await src.poll_once()
    assert [e.op for e in events] == [Op.DELETE]
    assert events[0].meta["via"] == "polling.tombstone"


async def test_polling_snapshot_marks_seen_keys() -> None:
    fetcher = ListRowFetcher()
    src = PollingSource(fetcher, ["books"])
    fetcher.set_rows(
        "books",
        [
            {"id": "b1", "__updated_at_micros": 50, "deleted_at": None},
            {"id": "b2", "__updated_at_micros": 60, "deleted_at": None},
        ],
    )
    snap = [e async for e in src.snapshot()]
    assert [e.op for e in snap] == [Op.READ, Op.READ]
    # After snapshot, a later change to b1 is an UPDATE (key already seen).
    fetcher.upsert("books", {"id": "b1", "__updated_at_micros": 100, "deleted_at": None})
    changes = await src.poll_once()
    assert [e.op for e in changes] == [Op.UPDATE]


# --------------------------------------------------------------------------- #
# ReplayBuffer
# --------------------------------------------------------------------------- #
def test_replay_buffer_serves_tail() -> None:
    buf = ReplayBuffer(capacity=4)
    src = FakeChangeStream()
    e1 = src.push_insert("books", {"id": "b1"})
    e2 = src.push_insert("books", {"id": "b2"})
    buf.append(e1)
    buf.append(e2)
    assert buf.replay_after(e1.position) == [e2]
    assert len(buf) == 2
