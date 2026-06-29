"""An Iceberg-shaped table catalog: snapshots, snapshot isolation, time-travel.

The catalog is the metadata layer over the columnar files. Its shape mirrors
Apache Iceberg, simplified to what the warehouse needs and made fully deterministic:

* :class:`DataFile` — metadata for one columnar file: its blob key, partition tuple,
  row count, and per-column statistics (lifted from the file footer for partition +
  file pruning *without* fetching the bytes).
* :class:`ManifestEntry` — a data file plus its add/delete status in a snapshot.
* :class:`Snapshot` — an immutable, monotonically-versioned set of live data files
  (a manifest), with a parent pointer, a timestamp, and a summary. A table's history
  is a linked list of snapshots.
* :class:`TableMetadata` — schema + partition spec + the snapshot log + current id.
* :class:`CatalogTable` — implements the :class:`~app.lakehouse.warehouse.contracts.Table`
  contract: append/overwrite/delete produce new snapshots; ``scan`` reads the
  current snapshot (or any prior one — *time-travel*) with partition + statistics
  pruning before decoding.
* :class:`Catalog` — the namespace of tables over a :class:`BlobStore`.

**Snapshot isolation.** Every mutation reads the table's current snapshot id, builds
a new file set, and commits a new snapshot whose ``parent_id`` is that read id. A
:class:`Transaction` lets several appends commit atomically as one snapshot. A
concurrent commit that no longer points at the latest snapshot raises
:class:`ConcurrentCommitError` (optimistic concurrency) — the writer rebases and
retries. Readers always see a consistent snapshot and never observe a partial commit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.blobstore import BlobStore, InMemoryBlobStore
from app.lakehouse.warehouse.columnar import ColumnarFile
from app.lakehouse.warehouse.partition import PartitionSpec, partition_key
from app.lakehouse.warehouse.predicate import Predicate
from app.lakehouse.warehouse.statistics import ColumnStatistics
from app.lakehouse.warehouse.types import ColumnVector, Schema


class ConcurrentCommitError(RuntimeError):
    """Raised when an optimistic commit's base snapshot is no longer current."""


class SnapshotNotFoundError(KeyError):
    """Raised when a requested time-travel snapshot id does not exist."""


@dataclass(frozen=True, slots=True)
class DataFile:
    """Metadata for one columnar file in the warehouse."""

    blob_key: str
    partition: tuple[Any, ...]
    record_count: int
    file_size_bytes: int
    column_stats: dict[str, ColumnStatistics]

    @property
    def partition_key(self) -> str:
        return partition_key(self.partition)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable, versioned set of live data files."""

    snapshot_id: int
    parent_id: int | None
    timestamp_ms: int
    data_files: tuple[DataFile, ...]
    summary: dict[str, str] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return sum(f.record_count for f in self.data_files)


@dataclass(frozen=True, slots=True)
class TableMetadata:
    """The full versioned metadata of a table (schema + spec + snapshot log)."""

    name: str
    schema: Schema
    partition_spec: PartitionSpec
    snapshots: tuple[Snapshot, ...]
    current_snapshot_id: int | None
    next_snapshot_id: int

    def snapshot(self, snapshot_id: int) -> Snapshot:
        for s in self.snapshots:
            if s.snapshot_id == snapshot_id:
                return s
        raise SnapshotNotFoundError(snapshot_id)

    def current(self) -> Snapshot | None:
        if self.current_snapshot_id is None:
            return None
        return self.snapshot(self.current_snapshot_id)

    def snapshot_as_of(self, timestamp_ms: int) -> Snapshot | None:
        """The latest snapshot committed at or before ``timestamp_ms``."""
        candidates = [s for s in self.snapshots if s.timestamp_ms <= timestamp_ms]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.timestamp_ms, s.snapshot_id))


def _clock_ms() -> int:
    return int(time.time() * 1000)


class CatalogTable:
    """A versioned warehouse table backed by a blob store (implements ``Table``)."""

    def __init__(
        self,
        metadata: TableMetadata,
        blobstore: BlobStore,
        *,
        rows_per_group: int = 4096,
        zone_size: int | None = None,
        clock: Any = _clock_ms,
    ) -> None:
        self._meta = metadata
        self._blobs = blobstore
        self._rows_per_group = rows_per_group
        self._zone_size = zone_size
        self._clock = clock

    # -- contract surface -----------------------------------------------------

    @property
    def name(self) -> str:
        return self._meta.name

    @property
    def schema(self) -> Schema:
        return self._meta.schema

    @property
    def partition_spec(self) -> PartitionSpec:
        return self._meta.partition_spec

    @property
    def metadata(self) -> TableMetadata:
        return self._meta

    def current_snapshot_id(self) -> int | None:
        return self._meta.current_snapshot_id

    def history(self) -> list[Snapshot]:
        return list(self._meta.snapshots)

    # -- writes (each produces a new snapshot) --------------------------------

    def append(self, batch: RecordBatch, *, summary: dict[str, str] | None = None) -> Snapshot:
        """Append rows as one or more new data files; commit a new snapshot."""
        base = self._meta.current_snapshot_id
        new_files = self._write_files(batch)
        existing = self._live_files(base)
        return self._commit(base, existing + new_files, "append", batch.num_rows, summary)

    def overwrite(self, batch: RecordBatch, *, summary: dict[str, str] | None = None) -> Snapshot:
        """Replace every live file with the new batch's files; commit a snapshot."""
        base = self._meta.current_snapshot_id
        new_files = self._write_files(batch)
        return self._commit(base, new_files, "overwrite", batch.num_rows, summary)

    def delete_where(
        self, predicate: Predicate, *, summary: dict[str, str] | None = None
    ) -> Snapshot:
        """Rewrite live files dropping rows matching ``predicate``; commit a snapshot.

        Files whose statistics prove no matching row are kept untouched (no
        rewrite); only overlapping files are decoded, filtered, and re-written.
        """
        base = self._meta.current_snapshot_id
        kept: list[DataFile] = []
        deleted = 0
        for df in self._live_files(base):
            if df.column_stats and predicate.can_skip_statistics(df.column_stats):
                kept.append(df)
                continue
            file = self._read_file(df)
            keep_batches: list[RecordBatch] = []
            for b in file.scan():
                mask = predicate.evaluate(b.mapping())
                deleted += sum(1 for m in mask if m)
                keep_batches.append(b.filter_mask([not m for m in mask]))
            survivors = [b for b in keep_batches if b.num_rows > 0]
            if survivors:
                merged = RecordBatch.concat(survivors)
                kept.extend(self._write_files(merged))
        return self._commit(base, kept, "delete", -deleted, summary)

    # -- reads ----------------------------------------------------------------

    def scan(
        self,
        *,
        columns: list[str] | None = None,
        predicate: Predicate | None = None,
        snapshot_id: int | None = None,
    ) -> list[RecordBatch]:
        """Read the (optionally historical) snapshot with pruning + the filter."""
        target = snapshot_id if snapshot_id is not None else self._meta.current_snapshot_id
        files = self._live_files(target)
        out: list[RecordBatch] = []
        for df in files:
            if (
                predicate is not None
                and df.column_stats
                and predicate.can_skip_statistics(df.column_stats)
            ):
                continue
            file = self._read_file(df)
            out.extend(file.scan(predicate=predicate, columns=columns))
        if not out:
            proj = columns if columns is not None else self._meta.schema.names
            return [RecordBatch.empty(self._meta.schema.select(proj))]
        return out

    def scan_as_of_timestamp(
        self,
        timestamp_ms: int,
        *,
        columns: list[str] | None = None,
        predicate: Predicate | None = None,
    ) -> list[RecordBatch]:
        snap = self._meta.snapshot_as_of(timestamp_ms)
        if snap is None:
            proj = columns if columns is not None else self._meta.schema.names
            return [RecordBatch.empty(self._meta.schema.select(proj))]
        return self.scan(columns=columns, predicate=predicate, snapshot_id=snap.snapshot_id)

    # -- maintenance ----------------------------------------------------------

    def rollback_to(self, snapshot_id: int) -> Snapshot:
        """Set the current snapshot back to ``snapshot_id`` (a new history entry)."""
        target = self._meta.snapshot(snapshot_id)
        new_id = self._meta.next_snapshot_id
        snap = Snapshot(
            snapshot_id=new_id,
            parent_id=self._meta.current_snapshot_id,
            timestamp_ms=int(self._clock()),
            data_files=target.data_files,
            summary={"operation": "rollback", "to": str(snapshot_id)},
        )
        self._meta = replace(
            self._meta,
            snapshots=self._meta.snapshots + (snap,),
            current_snapshot_id=new_id,
            next_snapshot_id=new_id + 1,
        )
        return snap

    def expire_snapshots(self, *, keep_last: int = 1) -> list[int]:
        """Drop the metadata of all but the last ``keep_last`` snapshots.

        The current snapshot is always retained. Returns the expired ids. (Blob
        GC of files no longer referenced by any retained snapshot is a follow-on.)
        """
        if keep_last < 1:
            raise ValueError("keep_last must be >= 1")
        ordered = sorted(self._meta.snapshots, key=lambda s: s.snapshot_id)
        retain = {s.snapshot_id for s in ordered[-keep_last:]}
        if self._meta.current_snapshot_id is not None:
            retain.add(self._meta.current_snapshot_id)
        expired = [s.snapshot_id for s in ordered if s.snapshot_id not in retain]
        self._meta = replace(
            self._meta,
            snapshots=tuple(s for s in self._meta.snapshots if s.snapshot_id in retain),
        )
        return expired

    # -- internals ------------------------------------------------------------

    def _live_files(self, snapshot_id: int | None) -> list[DataFile]:
        if snapshot_id is None:
            return []
        return list(self._meta.snapshot(snapshot_id).data_files)

    def _write_files(self, batch: RecordBatch) -> list[DataFile]:
        """Partition the batch and write one columnar file per partition."""
        if batch.schema != self._meta.schema:
            raise ValueError("batch schema does not match table schema")
        spec = self._meta.partition_spec
        if spec.is_unpartitioned:
            return [self._write_one(batch.mapping(), ())]
        groups: dict[str, tuple[tuple[Any, ...], list[int]]] = {}
        rows = batch.rows()
        for i, row in enumerate(rows):
            ptuple = spec.partition_value(row)
            key = partition_key(ptuple)
            groups.setdefault(key, (ptuple, []))[1].append(i)
        files: list[DataFile] = []
        for _key, (ptuple, idx) in sorted(groups.items()):
            cols = {name: batch.column(name).take(idx) for name in batch.schema.names}
            files.append(self._write_one(cols, ptuple))
        return files

    def _write_one(self, cols: dict[str, ColumnVector], ptuple: tuple[Any, ...]) -> DataFile:
        file = ColumnarFile.from_columns(
            self._meta.schema, cols, rows_per_group=self._rows_per_group, zone_size=self._zone_size
        )
        blob = file.serialize()
        key = self._blobs.put(blob)
        return DataFile(
            blob_key=key,
            partition=ptuple,
            record_count=file.num_rows,
            file_size_bytes=len(blob),
            column_stats=file.file_statistics(),
        )

    def _read_file(self, df: DataFile) -> ColumnarFile:
        return ColumnarFile.deserialize(self._blobs.get(df.blob_key))

    def _commit(
        self,
        base_id: int | None,
        files: list[DataFile],
        operation: str,
        delta_rows: int,
        summary: dict[str, str] | None,
    ) -> Snapshot:
        # Optimistic concurrency: the table object is single-writer in-process, but
        # the base must still equal the live current snapshot at commit time.
        if base_id != self._meta.current_snapshot_id:
            raise ConcurrentCommitError(
                f"base snapshot {base_id} != current {self._meta.current_snapshot_id}"
            )
        new_id = self._meta.next_snapshot_id
        merged_summary = {"operation": operation, "delta_rows": str(delta_rows)}
        if summary:
            merged_summary.update(summary)
        snap = Snapshot(
            snapshot_id=new_id,
            parent_id=base_id,
            timestamp_ms=int(self._clock()),
            data_files=tuple(files),
            summary=merged_summary,
        )
        self._meta = replace(
            self._meta,
            snapshots=self._meta.snapshots + (snap,),
            current_snapshot_id=new_id,
            next_snapshot_id=new_id + 1,
        )
        return snap


class Catalog:
    """A namespace of versioned tables over a shared blob store."""

    def __init__(self, blobstore: BlobStore | None = None, *, clock: Any = _clock_ms) -> None:
        self._blobs = blobstore if blobstore is not None else InMemoryBlobStore()
        self._tables: dict[str, CatalogTable] = {}
        self._clock = clock

    @property
    def blobstore(self) -> BlobStore:
        return self._blobs

    def create_table(
        self,
        name: str,
        schema: Schema,
        *,
        partition_spec: PartitionSpec | None = None,
        rows_per_group: int = 4096,
        zone_size: int | None = None,
        if_not_exists: bool = False,
    ) -> CatalogTable:
        if name in self._tables:
            if if_not_exists:
                return self._tables[name]
            raise ValueError(f"table {name!r} already exists")
        spec = partition_spec or PartitionSpec.unpartitioned()
        spec.validate(schema)
        meta = TableMetadata(
            name=name,
            schema=schema,
            partition_spec=spec,
            snapshots=(),
            current_snapshot_id=None,
            next_snapshot_id=1,
        )
        table = CatalogTable(
            meta, self._blobs, rows_per_group=rows_per_group, zone_size=zone_size, clock=self._clock
        )
        self._tables[name] = table
        return table

    def table(self, name: str) -> CatalogTable:
        try:
            return self._tables[name]
        except KeyError as exc:
            raise KeyError(f"no such table {name!r}") from exc

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def drop_table(self, name: str) -> None:
        self._tables.pop(name, None)

    def list_tables(self) -> list[str]:
        return sorted(self._tables)


__all__ = [
    "Catalog",
    "CatalogTable",
    "ConcurrentCommitError",
    "DataFile",
    "Snapshot",
    "SnapshotNotFoundError",
    "TableMetadata",
]
