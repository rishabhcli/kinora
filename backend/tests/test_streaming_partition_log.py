"""PartitionLog tests — offsets, fetch windows, retention, compaction, tombstones."""

from __future__ import annotations

import pytest

from app.streaming.log.errors import OffsetOutOfRangeError, RecordTooLargeError
from app.streaming.log.partition import PartitionLog
from app.streaming.log.topic import CleanupPolicy, RetentionPolicy, TopicConfig


def _log(config: TopicConfig) -> PartitionLog:
    return PartitionLog(config.name, 0, config)


def test_append_assigns_monotonic_offsets() -> None:
    log = _log(TopicConfig.deleted("t"))
    offsets = [log.append(key=None, value=b"x", timestamp_ms=i).offset for i in range(5)]
    assert offsets == [0, 1, 2, 3, 4]
    assert log.log_start_offset == 0
    assert log.log_end_offset == 5
    assert len(log) == 5


def test_fetch_window_and_resume() -> None:
    log = _log(TopicConfig.deleted("t"))
    for i in range(10):
        log.append(key=None, value=bytes([i]), timestamp_ms=i)
    result = log.fetch(0, max_records=4)
    assert [r.offset for r in result.records] == [0, 1, 2, 3]
    assert result.next_offset == 4
    assert result.high_watermark == 10
    nxt = log.fetch(result.next_offset, max_records=4)
    assert [r.offset for r in nxt.records] == [4, 5, 6, 7]


def test_fetch_at_end_offset_is_empty() -> None:
    log = _log(TopicConfig.deleted("t"))
    log.append(key=None, value=b"x", timestamp_ms=1)
    result = log.fetch(1)
    assert result.records == ()
    assert result.next_offset == 1


def test_fetch_out_of_range_raises_with_window() -> None:
    log = _log(TopicConfig.deleted("t"))
    log.append(key=None, value=b"x", timestamp_ms=1)
    with pytest.raises(OffsetOutOfRangeError) as exc:
        log.fetch(99)
    assert exc.value.log_start == 0
    assert exc.value.log_end == 1


def test_fetch_max_bytes_returns_at_least_one() -> None:
    log = _log(TopicConfig.deleted("t"))
    for i in range(5):
        log.append(key=None, value=b"x" * 100, timestamp_ms=i)
    result = log.fetch(0, max_bytes=10)  # below one record's size
    assert len(result.records) == 1  # always make progress


def test_record_too_large_rejected() -> None:
    log = _log(TopicConfig.deleted("t", max_message_bytes=8))
    with pytest.raises(RecordTooLargeError):
        log.append(key=None, value=b"x" * 100, timestamp_ms=1)


def test_segment_rolls_at_segment_bytes() -> None:
    config = TopicConfig(
        name="t",
        cleanup_policy=CleanupPolicy.DELETE,
        retention=RetentionPolicy(segment_bytes=10),
    )
    log = _log(config)
    for i in range(6):
        log.append(key=None, value=b"x" * 5, timestamp_ms=i)
    # 6 records * 5 bytes with a 10-byte roll → multiple segments, offsets intact.
    assert [r.offset for r in log.fetch(0).records] == [0, 1, 2, 3, 4, 5]


def test_retention_evicts_expired_whole_segments() -> None:
    config = TopicConfig(
        name="t",
        cleanup_policy=CleanupPolicy.DELETE,
        retention=RetentionPolicy(retention_ms=100, segment_bytes=1),
    )
    log = _log(config)
    for i in range(5):
        log.append(key=None, value=b"x", timestamp_ms=i * 10)  # ts 0,10,20,30,40
    removed = log.enforce_retention(now_ms=130)  # anything older than ts 30 expires
    assert removed >= 1
    assert log.log_start_offset > 0
    # The newest record is never evicted.
    assert log.read_one(log.log_end_offset - 1) is not None


def test_retention_never_evicts_active_segment() -> None:
    config = TopicConfig(
        name="t",
        cleanup_policy=CleanupPolicy.DELETE,
        retention=RetentionPolicy(retention_ms=0, segment_bytes=1024),
    )
    log = _log(config)
    log.append(key=None, value=b"x", timestamp_ms=0)
    removed = log.enforce_retention(now_ms=10_000)
    assert removed == 0  # single active segment is protected
    assert len(log) == 1


def test_compaction_keeps_latest_value_per_key() -> None:
    config = TopicConfig.compacted("t")
    log = _log(config)
    log.append(key=b"a", value=b"1", timestamp_ms=1)
    log.append(key=b"b", value=b"1", timestamp_ms=2)
    log.append(key=b"a", value=b"2", timestamp_ms=3)  # supersedes a=1
    log.append(key=b"a", value=b"3", timestamp_ms=4)  # supersedes a=2
    removed = log.compact(now_ms=1000)
    assert removed == 2
    surviving = {r.key: r.value for r in log.fetch(log.log_start_offset, max_records=100).records}
    assert surviving == {b"a": b"3", b"b": b"1"}
    # Offsets of survivors are preserved (gaps allowed).
    recs = log.fetch(log.log_start_offset, max_records=100).records
    a_offsets = [r.offset for r in recs if r.key == b"a"]
    assert a_offsets == [3]


def test_compaction_keeps_keyless_records() -> None:
    log = _log(TopicConfig.compacted("t"))
    log.append(key=None, value=b"x", timestamp_ms=1)
    log.append(key=b"a", value=b"1", timestamp_ms=2)
    log.append(key=b"a", value=b"2", timestamp_ms=3)
    log.compact(now_ms=1000)
    values = [r.value for r in log.fetch(log.log_start_offset, max_records=100).records]
    assert b"x" in values  # keyless never deduplicated


def test_tombstone_survives_grace_then_reaped() -> None:
    config = TopicConfig.compacted("t", delete_retention_ms=100)
    log = _log(config)
    log.append(key=b"a", value=b"1", timestamp_ms=0)
    log.append(key=b"a", value=None, timestamp_ms=10)  # tombstone deletes a

    # Within grace: the tombstone is retained so consumers observe the delete.
    log.compact(now_ms=50)
    recs = log.fetch(log.log_start_offset, max_records=100).records
    assert any(r.key == b"a" and r.value is None for r in recs)

    # After grace: the tombstone (and thus the key) is gone.
    log.compact(now_ms=500)
    recs = log.fetch(log.log_start_offset, max_records=100).records
    assert all(r.key != b"a" for r in recs)


def test_min_compaction_lag_protects_recent_tail() -> None:
    config = TopicConfig.compacted("t", min_compaction_lag_ms=100)
    log = _log(config)
    log.append(key=b"a", value=b"1", timestamp_ms=0)
    log.append(key=b"a", value=b"2", timestamp_ms=950)  # recent — within lag at now=1000
    removed = log.compact(now_ms=1000)
    assert removed == 0  # both recent enough to be in the protected tail
    log.append(key=b"a", value=b"3", timestamp_ms=1000)
    removed = log.compact(now_ms=2000)  # now the first two are old
    assert removed >= 1


def test_offset_for_timestamp() -> None:
    log = _log(TopicConfig.deleted("t"))
    for i in range(5):
        log.append(key=None, value=b"x", timestamp_ms=i * 100)  # 0,100,200,300,400
    assert log.offset_for_timestamp(150) == 2  # first ts >= 150 is ts=200 @ offset 2
    assert log.offset_for_timestamp(0) == 0
    assert log.offset_for_timestamp(9999) is None


def test_maintain_compacts_then_retains() -> None:
    config = TopicConfig.compacted("t", also_delete=True, delete_retention_ms=0)
    log = _log(config)
    log.append(key=b"a", value=b"1", timestamp_ms=0)
    log.append(key=b"a", value=b"2", timestamp_ms=1)
    removed = log.maintain(now_ms=1000)
    assert removed >= 1
