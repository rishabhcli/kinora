"""The watermark-based incremental ELT framework.

ELT extracts rows from the operational store (Postgres) and **loads** them into a
warehouse table, then any **transform** runs as warehouse queries downstream. This
module owns the *extract + load* half and the incremental bookkeeping.

Design
------
* A :class:`RowSource` is the seam over the operational store: given a cursor
  column and a watermark, it yields the next page of rows strictly *after* the
  watermark, ordered by the cursor. The real implementation wraps an async SQL
  query (``SELECT ... WHERE cursor > :wm ORDER BY cursor LIMIT :n``); tests use
  :class:`ListRowSource`. The framework never imports a DB driver, so it stays pure.
* An :class:`ExtractSpec` declares the source name, the warehouse table, the cursor
  column (an ordered, monotonic column such as ``updated_at`` or an autoincrement
  ``id``), the row→column mapping, and the load mode (``append`` / ``merge``).
* A :class:`WatermarkStore` persists the last successfully-loaded cursor value per
  pipeline so a re-run resumes incrementally. :class:`InMemoryWatermarkStore` is the
  deterministic default.
* :class:`EltPipeline.run` pulls pages until the source is exhausted, converts each
  page to a :class:`~app.lakehouse.warehouse.batch.RecordBatch`, loads it into the
  warehouse table (a new snapshot per run — snapshot isolation), and advances the
  watermark. It returns an :class:`EltRunResult` (rows read, batches loaded, the new
  watermark, the snapshot id) — the observable contract a scheduler/worker drives.

``merge`` mode dedupes on a declared key by keeping the last row per key seen across
the run *and* the table's existing rows (an upsert): existing rows whose key is
re-extracted are replaced. This makes the pipeline **idempotent** — re-running over
overlapping data converges to the same table state.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.catalog import CatalogTable, Snapshot
from app.lakehouse.warehouse.predicate import InList
from app.lakehouse.warehouse.types import ColumnVector, Schema


class LoadMode(enum.StrEnum):
    APPEND = "append"
    MERGE = "merge"  # upsert on a key column


@runtime_checkable
class RowSource(Protocol):
    """Yields operational rows after a watermark, ordered by the cursor column.

    The real implementation issues a keyset-paginated SQL query; this protocol keeps
    the framework driver-free and unit-testable.
    """

    def fetch_after(
        self, cursor_column: str, watermark: Any | None, limit: int
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` rows with ``cursor_column > watermark``, ordered."""
        ...


class ListRowSource:
    """A deterministic in-memory :class:`RowSource` over a list of row dicts.

    Supports live mutation (:meth:`add_row` / :meth:`set_rows`) so tests can model
    the operational store changing between incremental runs.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def fetch_after(
        self, cursor_column: str, watermark: Any | None, limit: int
    ) -> list[dict[str, Any]]:
        ordered = sorted(self._rows, key=lambda r: r[cursor_column])
        if watermark is not None:
            ordered = [r for r in ordered if r[cursor_column] > watermark]
        return ordered[:limit]

    def add_row(self, row: dict[str, Any]) -> None:
        self._rows.append(row)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)


@runtime_checkable
class WatermarkStore(Protocol):
    """Persists the last-loaded cursor value per pipeline."""

    def get(self, pipeline: str) -> Any | None: ...

    def set(self, pipeline: str, watermark: Any) -> None: ...


class InMemoryWatermarkStore:
    """A deterministic in-memory watermark store."""

    def __init__(self) -> None:
        self._wm: dict[str, Any] = {}

    def get(self, pipeline: str) -> Any | None:
        return self._wm.get(pipeline)

    def set(self, pipeline: str, watermark: Any) -> None:
        self._wm[pipeline] = watermark


@dataclass(frozen=True, slots=True)
class ExtractSpec:
    """Declares one source→warehouse extraction."""

    pipeline_name: str
    source_name: str
    target_table: str
    cursor_column: str
    # Maps a warehouse column name -> the source row key it reads from.
    column_map: dict[str, str]
    schema: Schema
    load_mode: LoadMode = LoadMode.APPEND
    merge_key: str | None = None  # required for MERGE
    page_size: int = 1000
    max_pages: int = 1_000_000

    def __post_init__(self) -> None:
        if self.load_mode is LoadMode.MERGE and not self.merge_key:
            raise ValueError("MERGE load mode requires a merge_key")
        if self.merge_key and not self.schema.has(self.merge_key):
            raise ValueError(f"merge_key {self.merge_key} not in target schema")
        for name in self.schema.names:
            if name not in self.column_map:
                raise ValueError(f"column_map missing target column {name}")

    def cursor_source_key(self) -> str:
        """The source-row key whose value is the cursor (must be in the map values)."""
        for target, source in self.column_map.items():
            if source == self.cursor_column or target == self.cursor_column:
                return source
        # Cursor may be a raw source column not mapped to the warehouse.
        return self.cursor_column


@dataclass(frozen=True, slots=True)
class EltRunResult:
    """The observable outcome of one pipeline run."""

    pipeline_name: str
    rows_read: int
    pages_read: int
    rows_loaded: int
    new_watermark: Any | None
    snapshot: Snapshot | None
    summary: dict[str, str] = field(default_factory=dict)

    @property
    def made_progress(self) -> bool:
        return self.rows_read > 0


class EltPipeline:
    """Runs one :class:`ExtractSpec` incrementally into a warehouse table."""

    def __init__(
        self,
        spec: ExtractSpec,
        source: RowSource,
        target: CatalogTable,
        watermarks: WatermarkStore,
    ) -> None:
        if target.schema != spec.schema:
            raise ValueError("target table schema does not match ExtractSpec schema")
        self._spec = spec
        self._source = source
        self._target = target
        self._wm = watermarks

    def run(self) -> EltRunResult:
        spec = self._spec
        watermark = self._wm.get(spec.pipeline_name)
        cursor_src = spec.cursor_source_key()

        collected: list[dict[str, Any]] = []
        pages = 0
        last_cursor = watermark
        for page in self._pages(cursor_src, watermark):
            pages += 1
            collected.extend(page)
            last_cursor = page[-1][cursor_src]

        if not collected:
            return EltRunResult(
                pipeline_name=spec.pipeline_name,
                rows_read=0,
                pages_read=0,
                rows_loaded=0,
                new_watermark=watermark,
                snapshot=None,
                summary={"status": "no-new-rows"},
            )

        batch = self._to_batch(collected)
        snapshot, loaded = self._load(batch)
        self._wm.set(spec.pipeline_name, last_cursor)
        return EltRunResult(
            pipeline_name=spec.pipeline_name,
            rows_read=len(collected),
            pages_read=pages,
            rows_loaded=loaded,
            new_watermark=last_cursor,
            snapshot=snapshot,
            summary={"status": "loaded", "mode": spec.load_mode.value},
        )

    def run_to_exhaustion(self, *, max_runs: int = 10_000) -> list[EltRunResult]:
        """Repeatedly :meth:`run` until a run makes no progress.

        Useful when ``max_pages`` caps a single run; each run advances the watermark
        so the next resumes where it left off. Returns every run's result.
        """
        results: list[EltRunResult] = []
        for _ in range(max_runs):
            res = self.run()
            results.append(res)
            if not res.made_progress:
                break
        return results

    # -- internals ------------------------------------------------------------

    def _pages(self, cursor_src: str, watermark: Any | None) -> Iterator[list[dict[str, Any]]]:
        spec = self._spec
        wm = watermark
        for _ in range(spec.max_pages):
            page = self._source.fetch_after(cursor_src, wm, spec.page_size)
            if not page:
                return
            yield page
            wm = page[-1][cursor_src]
            if len(page) < spec.page_size:
                return

    def _to_batch(self, rows: list[dict[str, Any]]) -> RecordBatch:
        spec = self._spec
        columns: dict[str, ColumnVector] = {}
        for fld in spec.schema.fields:
            source_key = spec.column_map[fld.name]
            values = [row.get(source_key) for row in rows]
            columns[fld.name] = ColumnVector.from_pylist(fld.dtype, values)
        return RecordBatch.from_mapping(spec.schema, columns)

    def _load(self, batch: RecordBatch) -> tuple[Snapshot, int]:
        spec = self._spec
        if spec.load_mode is LoadMode.APPEND:
            snap = self._target.append(batch, summary={"pipeline": spec.pipeline_name})
            return snap, batch.num_rows
        return self._merge(batch)

    def _merge(self, batch: RecordBatch) -> tuple[Snapshot, int]:
        """Upsert ``batch`` on ``merge_key``: replace existing rows with the same key.

        Within the incoming batch, the *last* row per key wins (the source is ordered
        by the cursor, so this is last-write-wins). Existing rows whose key collides
        are deleted before the new rows are appended — in one atomic operation we
        overwrite the affected partitions' equivalent: here we delete-by-key then
        append, producing two snapshots; the final state is idempotent.
        """
        spec = self._spec
        key = spec.merge_key
        assert key is not None  # guarded by ExtractSpec
        # Dedupe the incoming batch keeping the last occurrence per key.
        seen: dict[Any, int] = {}
        key_vec = batch.column(key)
        for i in range(batch.num_rows):
            seen[key_vec.get(i)] = i
        keep_idx = sorted(seen.values())
        deduped = batch.take(keep_idx)

        incoming_keys = [deduped.column(key).get(i) for i in range(deduped.num_rows)]
        present_keys = [k for k in incoming_keys if k is not None]
        if present_keys:
            self._target.delete_where(
                InList(key, tuple(present_keys)),
                summary={"pipeline": spec.pipeline_name, "phase": "merge-delete"},
            )
        snap = self._target.append(
            deduped, summary={"pipeline": spec.pipeline_name, "phase": "merge-append"}
        )
        return snap, deduped.num_rows


__all__ = [
    "EltPipeline",
    "EltRunResult",
    "ExtractSpec",
    "InMemoryWatermarkStore",
    "ListRowSource",
    "LoadMode",
    "RowSource",
    "WatermarkStore",
]
