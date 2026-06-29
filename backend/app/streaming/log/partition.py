"""The append log of a single partition — offsets, segments, retention, compaction.

This is the substrate's core data structure: an ordered, append-only sequence of
:class:`~app.streaming.log.record.ConsumerRecord` per partition, addressed by a
monotonic ``offset``. It models Kafka's per-partition log faithfully enough to
exercise every behaviour the higher layers depend on:

* **Offsets.** ``log_start_offset`` (oldest retained) and ``log_end_offset`` (next
  offset to be assigned) bound the readable window. Appends assign the current
  end offset then advance it; offsets are never reused even after retention
  evicts records, so a consumer that committed offset *N* keeps a stable meaning.
* **Segments.** Records live in roll-bounded :class:`_Segment`\\ s (Kafka rolls a
  new segment at ``segment_bytes``). Retention deletes *whole* segments only, so
  ``log_start_offset`` jumps in segment-sized steps — exactly Kafka's behaviour.
* **Retention (DELETE policy).** :meth:`enforce_retention` drops segments whose
  newest record is older than ``retention_ms`` and trims oldest segments while
  total bytes exceed ``retention_bytes``. The active (last) segment is never
  deleted.
* **Compaction (COMPACT policy).** :meth:`compact` rewrites the log keeping only
  the latest record per key at/after the *compaction point* (records younger than
  ``min_compaction_lag_ms`` are never compacted, preserving the tail's order).
  Tombstones (``value is None``) survive for ``delete_retention_ms`` so consumers
  observe deletes, then are reaped.

This object is **not** itself thread-safe; the broker serialises access per
partition (the in-memory broker with an ``asyncio.Lock``, Redis natively).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from app.streaming.log.errors import (
    OffsetOutOfRangeError,
    RecordTooLargeError,
)
from app.streaming.log.record import ConsumerRecord, Headers
from app.streaming.log.topic import TopicConfig

__all__ = ["FetchResult", "PartitionLog"]


def _record_bytes(key: bytes | None, value: bytes | None, headers: Headers) -> int:
    """Approximate serialized size of a record (key + value + header bytes)."""
    size = len(key or b"") + len(value or b"")
    for hkey, hval in headers:
        size += len(hkey.encode("utf-8")) + len(hval)
    return size


@dataclass(slots=True)
class _StoredRecord:
    """A record plus its cached on-disk size (kept off the hot path)."""

    record: ConsumerRecord
    size: int


@dataclass(slots=True)
class _Segment:
    """A roll-bounded run of records. Retention deletes whole segments."""

    base_offset: int
    records: list[_StoredRecord] = field(default_factory=list)
    bytes: int = 0

    @property
    def next_offset(self) -> int:
        """Offset that would follow the last record in this segment."""
        if not self.records:
            return self.base_offset
        return self.records[-1].record.offset + 1

    @property
    def max_timestamp(self) -> int:
        """Newest record timestamp in the segment (0 if empty)."""
        return max((r.record.timestamp_ms for r in self.records), default=0)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of a :meth:`PartitionLog.fetch`.

    ``records`` are in offset order; ``next_offset`` is where to resume (the
    offset after the last returned record, or the requested offset if empty);
    ``high_watermark`` is the partition's current ``log_end_offset`` so a
    consumer can compute lag without a second call.
    """

    records: tuple[ConsumerRecord, ...]
    next_offset: int
    high_watermark: int
    log_start_offset: int


class PartitionLog:
    """The append-only record log of one partition of one topic."""

    def __init__(self, topic: str, partition: int, config: TopicConfig) -> None:
        self._topic = topic
        self._partition = partition
        self._config = config
        self._log_start_offset = 0
        self._log_end_offset = 0
        # At least one (active) segment always exists.
        self._segments: list[_Segment] = [_Segment(base_offset=0)]

    # --- coordinates ----------------------------------------------------- #

    @property
    def topic(self) -> str:
        """The owning topic name."""
        return self._topic

    @property
    def partition(self) -> int:
        """This partition's index within its topic."""
        return self._partition

    @property
    def log_start_offset(self) -> int:
        """Oldest offset still retained (advances past evicted/compacted records)."""
        return self._log_start_offset

    @property
    def log_end_offset(self) -> int:
        """Next offset to be assigned (a.k.a. the high watermark for this single-replica log)."""
        return self._log_end_offset

    @property
    def high_watermark(self) -> int:
        """Highest readable offset boundary — equals ``log_end_offset`` here."""
        return self._log_end_offset

    def __len__(self) -> int:
        """Number of records currently retained."""
        return sum(len(s.records) for s in self._segments)

    @property
    def size_bytes(self) -> int:
        """Total retained record bytes."""
        return sum(s.bytes for s in self._segments)

    # --- append ---------------------------------------------------------- #

    def append(
        self,
        *,
        key: bytes | None,
        value: bytes | None,
        timestamp_ms: int,
        headers: Headers = (),
    ) -> ConsumerRecord:
        """Append one record at the current end offset; return the stored record.

        Rolls a new segment first if the active one has reached ``segment_bytes``.
        Raises :class:`RecordTooLargeError` if the record exceeds the topic's
        ``max_message_bytes``.
        """
        size = _record_bytes(key, value, headers)
        if size > self._config.max_message_bytes:
            raise RecordTooLargeError(size, self._config.max_message_bytes)

        offset = self._log_end_offset
        record = ConsumerRecord(
            topic=self._topic,
            partition=self._partition,
            offset=offset,
            timestamp_ms=timestamp_ms,
            key=key,
            value=value,
            headers=headers,
        )
        active = self._segments[-1]
        if active.records and active.bytes >= self._config.retention.segment_bytes:
            active = _Segment(base_offset=offset)
            self._segments.append(active)
        active.records.append(_StoredRecord(record=record, size=size))
        active.bytes += size
        self._log_end_offset = offset + 1
        return record

    # --- fetch ----------------------------------------------------------- #

    def _locate(self, offset: int) -> tuple[int, int]:
        """Return ``(segment_index, record_index)`` for the first offset >= ``offset``.

        Uses the segment base-offset index for O(log n) seek into the right
        segment, then a linear scan within it.
        """
        bases = [s.base_offset for s in self._segments]
        seg_idx = max(0, bisect.bisect_right(bases, offset) - 1)
        segment = self._segments[seg_idx]
        for rec_idx, stored in enumerate(segment.records):
            if stored.record.offset >= offset:
                return seg_idx, rec_idx
        # Offset is at/after this segment's end — start of the next segment.
        return seg_idx, len(segment.records)

    def fetch(
        self, offset: int, *, max_records: int = 500, max_bytes: int | None = None
    ) -> FetchResult:
        """Read up to ``max_records`` (and ``max_bytes``) starting at ``offset``.

        ``offset == log_end_offset`` is valid and returns an empty result (the
        consumer is caught up). An offset below ``log_start_offset`` or above
        ``log_end_offset`` raises :class:`OffsetOutOfRangeError` so the caller
        can apply its reset policy.
        """
        if offset < self._log_start_offset or offset > self._log_end_offset:
            raise OffsetOutOfRangeError(
                self._topic,
                self._partition,
                offset,
                self._log_start_offset,
                self._log_end_offset,
            )
        if offset == self._log_end_offset:
            return FetchResult(
                records=(),
                next_offset=offset,
                high_watermark=self._log_end_offset,
                log_start_offset=self._log_start_offset,
            )

        out: list[ConsumerRecord] = []
        accumulated = 0
        seg_idx, rec_idx = self._locate(offset)
        next_offset = offset
        while seg_idx < len(self._segments) and len(out) < max_records:
            segment = self._segments[seg_idx]
            while rec_idx < len(segment.records) and len(out) < max_records:
                stored = segment.records[rec_idx]
                if max_bytes is not None and out and accumulated + stored.size > max_bytes:
                    return FetchResult(
                        records=tuple(out),
                        next_offset=next_offset,
                        high_watermark=self._log_end_offset,
                        log_start_offset=self._log_start_offset,
                    )
                out.append(stored.record)
                accumulated += stored.size
                next_offset = stored.record.offset + 1
                rec_idx += 1
            seg_idx += 1
            rec_idx = 0
        return FetchResult(
            records=tuple(out),
            next_offset=next_offset,
            high_watermark=self._log_end_offset,
            log_start_offset=self._log_start_offset,
        )

    def read_one(self, offset: int) -> ConsumerRecord | None:
        """Read the single record at exactly ``offset`` (``None`` if compacted away)."""
        result = self.fetch(offset, max_records=1)
        for record in result.records:
            if record.offset == offset:
                return record
        return None

    # --- offset reset helpers ------------------------------------------- #

    def earliest_offset(self) -> int:
        """The offset a consumer resetting to ``earliest`` should start at."""
        return self._log_start_offset

    def latest_offset(self) -> int:
        """The offset a consumer resetting to ``latest`` should start at."""
        return self._log_end_offset

    def offset_for_timestamp(self, timestamp_ms: int) -> int | None:
        """First offset whose record timestamp is >= ``timestamp_ms`` (Kafka offsetsForTimes)."""
        for segment in self._segments:
            if segment.max_timestamp < timestamp_ms:
                continue
            for stored in segment.records:
                if stored.record.timestamp_ms >= timestamp_ms:
                    return stored.record.offset
        return None

    # --- retention (DELETE policy) -------------------------------------- #

    def enforce_retention(self, now_ms: int) -> int:
        """Evict whole segments past the time/size bound; return records removed.

        The active (last) segment is never evicted, mirroring Kafka.
        """
        if not self._config.cleanup_policy.deletes:
            return 0
        removed = 0
        retention = self._config.retention

        # Age-based: drop leading segments whose newest record is expired.
        while len(self._segments) > 1:
            head = self._segments[0]
            if not head.records or not retention.is_expired(head.max_timestamp, now_ms):
                break
            removed += self._evict_head()

        # Size-based: trim leading segments while over the byte ceiling.
        if retention.size_bounded:
            while len(self._segments) > 1 and self.size_bytes > retention.retention_bytes:
                removed += self._evict_head()

        return removed

    def _evict_head(self) -> int:
        """Remove the oldest segment, advancing ``log_start_offset``; return count."""
        head = self._segments.pop(0)
        count = len(head.records)
        self._log_start_offset = self._segments[0].base_offset
        return count

    # --- compaction (COMPACT policy) ------------------------------------ #

    def compact(self, now_ms: int) -> int:
        """Keep only the latest record per key from the compaction point; return removed.

        Records newer than ``min_compaction_lag_ms`` form an un-compacted tail
        kept verbatim. Within the compactible head, only the *last* record per
        key survives (Kafka's "keep latest value"); tombstones are kept until
        ``delete_retention_ms`` has elapsed, then dropped so the key disappears.
        Records with no key are always retained (they can't be deduplicated).
        """
        if not self._config.cleanup_policy.compacts:
            return 0

        all_stored = [s for seg in self._segments for s in seg.records]
        if not all_stored:
            return 0

        lag = self._config.min_compaction_lag_ms
        compact_until_idx = len(all_stored)
        if lag > 0:
            for idx, stored in enumerate(all_stored):
                if (now_ms - stored.record.timestamp_ms) < lag:
                    compact_until_idx = idx
                    break

        head = all_stored[:compact_until_idx]
        tail = all_stored[compact_until_idx:]

        # Last index per key within the compactible head.
        last_index: dict[bytes, int] = {}
        for idx, stored in enumerate(head):
            if stored.record.key is not None:
                last_index[stored.record.key] = idx

        kept_head: list[_StoredRecord] = []
        for idx, stored in enumerate(head):
            rec = stored.record
            if rec.key is None:
                kept_head.append(stored)
                continue
            if last_index.get(rec.key) != idx:
                continue  # superseded by a later record for the same key
            # Tombstone: keep until its delete-retention grace elapses, then drop.
            grace = self._config.delete_retention_ms
            if rec.value is None and (now_ms - rec.timestamp_ms) >= grace:
                continue
            kept_head.append(stored)

        kept = kept_head + tail
        removed = len(all_stored) - len(kept)
        if removed:
            self._rebuild_segments(kept)
        return removed

    def _rebuild_segments(self, kept: list[_StoredRecord]) -> None:
        """Repack surviving records into fresh roll-bounded segments.

        Offsets are preserved (compaction leaves gaps — Kafka behaviour); only
        the storage layout is rebuilt. ``log_start_offset`` advances to the
        first surviving record's offset.
        """
        if not kept:
            # Everything compacted away: a single empty active segment at the end.
            self._segments = [_Segment(base_offset=self._log_end_offset)]
            self._log_start_offset = self._log_end_offset
            return

        seg_limit = self._config.retention.segment_bytes
        segments: list[_Segment] = [_Segment(base_offset=kept[0].record.offset)]
        for stored in kept:
            active = segments[-1]
            if active.records and active.bytes >= seg_limit:
                active = _Segment(base_offset=stored.record.offset)
                segments.append(active)
            active.records.append(stored)
            active.bytes += stored.size
        self._segments = segments
        self._log_start_offset = kept[0].record.offset

    # --- maintenance dispatch ------------------------------------------- #

    def maintain(self, now_ms: int) -> int:
        """Run whichever cleanup policies are configured; return total records removed.

        Compaction first (collapse history), then retention (age out the tail),
        matching Kafka's compact-then-delete ordering for ``COMPACT | DELETE``.
        """
        removed = 0
        if self._config.cleanup_policy.compacts:
            removed += self.compact(now_ms)
        if self._config.cleanup_policy.deletes:
            removed += self.enforce_retention(now_ms)
        return removed
