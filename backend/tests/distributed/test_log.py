"""Tests for the replication log (log.py)."""

from __future__ import annotations

import pytest

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.log import (
    OpKind,
    ReplicationLog,
    ReplicationRecord,
    SequenceGapError,
    WriteOp,
    merge_logs,
    truncate_record,
)
from app.distributed.replication.version import VersionVector

A = NodeId("us", "a")
B = NodeId("eu", "b")


def rec(
    origin: NodeId,
    seq: int,
    wall: int,
    key: str = "k",
    deps: VersionVector | None = None,
) -> ReplicationRecord:
    return ReplicationRecord(
        origin=origin,
        seq=seq,
        timestamp=HybridTimestamp(wall, 0, origin),
        op=WriteOp.set(key, f"v{seq}"),
        deps=deps or VersionVector.empty(),
    )


def test_writeop_constructors() -> None:
    assert WriteOp.set("k", 1) == WriteOp("k", OpKind.SET, 1)
    assert WriteOp.delete("k") == WriteOp("k", OpKind.DELETE, None)


def test_append_enforces_gapless_sequence() -> None:
    log = ReplicationLog()
    log.append(rec(A, 1, 10))
    log.append(rec(A, 2, 20))
    with pytest.raises(SequenceGapError):
        log.append(rec(A, 4, 40))  # skipped 3


def test_next_seq_tracks_per_origin() -> None:
    log = ReplicationLog()
    assert log.next_seq(A) == 1
    log.append(rec(A, 1, 10))
    assert log.next_seq(A) == 2
    assert log.next_seq(B) == 1


def test_high_water_covers_all_segments() -> None:
    log = ReplicationLog()
    log.append(rec(A, 1, 10))
    log.append(rec(A, 2, 20))
    log.append(rec(B, 1, 15))
    assert log.high_water() == VersionVector.of({A: 2, B: 1})


def test_records_after_returns_suffix() -> None:
    log = ReplicationLog()
    for i in range(1, 4):
        log.append(rec(A, i, i * 10))
    suffix = log.records_after(A, 1)
    assert [r.seq for r in suffix] == [2, 3]


def test_delta_since_is_causal_timestamp_ordered() -> None:
    log = ReplicationLog()
    log.append(rec(A, 1, 30))
    log.append(rec(B, 1, 10))
    log.append(rec(A, 2, 20))
    delta = log.delta_since(VersionVector.empty())
    # sorted by timestamp: B@10, A@20, A@30
    assert [(r.origin, r.timestamp.wall_ms) for r in delta] == [(B, 10), (A, 20), (A, 30)]


def test_delta_since_excludes_seen() -> None:
    log = ReplicationLog()
    log.append(rec(A, 1, 10))
    log.append(rec(A, 2, 20))
    delta = log.delta_since(VersionVector.of({A: 1}))
    assert [r.seq for r in delta] == [2]


def test_causally_ready_requires_deps_and_no_gap() -> None:
    deps = VersionVector.of({B: 2})
    record = ReplicationRecord(A, 3, HybridTimestamp(10, 0, A), WriteOp.set("k", 1), deps)
    # missing the dep on B
    assert not record.causally_ready(VersionVector.of({A: 2}))
    # has dep but gap in A's own segment (applied only seq 1, this is seq 3)
    assert not record.causally_ready(VersionVector.of({A: 1, B: 2}))
    # dep satisfied and contiguous
    assert record.causally_ready(VersionVector.of({A: 2, B: 2}))


def test_merge_logs_unions_and_is_idempotent() -> None:
    log1 = ReplicationLog()
    log1.append(rec(A, 1, 10))
    log1.append(rec(A, 2, 20))
    log2 = ReplicationLog()
    log2.append(rec(A, 1, 10))  # duplicate, identical
    log2.append(rec(B, 1, 15))
    merged = merge_logs([log1, log2])
    assert len(merged) == 3
    # idempotent: merging again changes nothing
    again = merge_logs([merged, merged])
    assert len(again) == 3


def test_merge_logs_detects_conflicting_records() -> None:
    log1 = ReplicationLog()
    log1.append(rec(A, 1, 10, key="x"))
    log2 = ReplicationLog()
    log2.append(rec(A, 1, 99, key="y"))  # same origin+seq, different content
    with pytest.raises(SequenceGapError):
        merge_logs([log1, log2])


def test_truncate_record_clips_long_string_values() -> None:
    long = ReplicationRecord(A, 1, HybridTimestamp(1, 0, A), WriteOp.set("k", "z" * 500))
    clipped = truncate_record(long, max_value_repr=10)
    assert len(clipped.op.value) == 11  # 10 chars + ellipsis
    # short values untouched
    short = rec(A, 1, 10)
    assert truncate_record(short) is short
