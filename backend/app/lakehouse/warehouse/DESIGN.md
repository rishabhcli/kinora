# Lakehouse — Warehouse facet (`app/lakehouse/warehouse/`)

A self-contained, deterministic **mini analytical lakehouse**: a typed columnar
storage format, an Iceberg-shaped table catalog with snapshot isolation +
time-travel, a watermark-based incremental ELT framework that extracts from the
operational Postgres, and a small vectorized query engine with a logical+physical
planner. It defines the `Table` / `QueryEngine` contracts the sibling lakehouse
facets (feature store, semantic layer) consume.

## Why a separate package (distinct from `app/analytics/`)

`app/analytics/` is the **operational product-analytics pipeline**: typed events →
sessionization → daily/period summary *rollup tables* in Postgres, answering "how
do humans use the product?". It is row-oriented, retained in the OLTP store, and
optimised for ingest + fixed rollups.

The lakehouse is the **analytical substrate beside/beneath it**: a columnar,
snapshot-versioned warehouse with its own storage format, query engine, and ELT.
It answers arbitrary analytical questions (scan / filter / project / aggregate /
group-by / join) over large historical data with column pruning, predicate
pushdown, and time-travel — the things a rollup table cannot do. The two are
complementary: `app/analytics` can become an *ELT source* for the warehouse.

## Module map

| Module | Responsibility |
|---|---|
| `types.py` | The logical type system (`LogicalType`, `Field`, `Schema`) and the nullable in-memory `ColumnVector` (values + null bitmap). The lingua franca of the whole stack. |
| `encoding.py` | Physical column codecs — PLAIN, DICTIONARY, RLE — over a hand-rolled `struct`/uvarint frame, byte-deterministic round-trips, and an automatic smallest-codec chooser. |
| `statistics.py` | Per-chunk `ColumnStatistics` (count / null-count / distinct / min / max) + `ZoneMap` (sub-chunk min/max zones) + `merge_statistics`. Drives pushdown. |
| `predicate.py` | The pushdown predicate algebra (`=,!=,<,<=,>,>=`, `IN`, `IS [NOT] NULL`, AND/OR/NOT). One definition serves both statistics-based skipping (`can_skip_statistics`) and vectorized evaluation (`evaluate`) with SQL three-valued NULL logic. |
| `column_chunk.py` | `ColumnChunk` — an encoded column + its statistics + optional zonemap; the smallest independently-readable, self-describing serialisable unit. |
| `row_group.py` | `RowGroup` — aligned column chunks for one horizontal slice; the unit pushdown skips (`can_skip`). |
| `batch.py` | `RecordBatch` — schema + aligned vectors; the value operators pass between each other (project / filter / take / slice / concat / rows). |
| `columnar.py` | `ColumnarFile` — row groups + a self-describing footer (schema + per-group stats); `scan()` applies predicate pushdown (skip groups) + the residual filter + projection. Serialises to a `KLWF` blob (Parquet-shaped trailing footer + magic). |
| `partition.py` | `PartitionSpec` / `PartitionField` transforms (identity, year/month/day/hour, bucket[N], truncate[W]) — Iceberg-shaped partition-value derivation for partition pruning. |
| `blobstore.py` | `BlobStore` seam (content-addressed) + `InMemoryBlobStore`; the catalog records keys, the bytes live here (OSS/MinIO in prod). |
| `catalog.py` | The Iceberg-shaped catalog: `DataFile`, `Snapshot`, `TableMetadata`, `CatalogTable` (append / overwrite / delete-where / scan / time-travel / rollback / expire), and `Catalog` (the table namespace). Optimistic-concurrency snapshot isolation (`ConcurrentCommitError`). |
| `contracts.py` | The `Table` / `QueryEngine` `Protocol`s + `TableScan` — the stable seam sibling facets type against. |
| `expr.py` | Vectorized scalar expressions: `Column`, `Literal`, `Arithmetic`, `Comparison`, `BoolOp` (3-valued), `Cast`, `Coalesce` — evaluated whole-column over a batch. |
| `aggregate.py` | Aggregate specs + foldable accumulators: COUNT(\*), COUNT, COUNT(DISTINCT), SUM, MIN, MAX, AVG. |
| `logical.py` | The logical plan tree (`Scan`, `Filter`, `Project`, `Aggregate`, `Join`, `Sort`, `Limit`) + a fluent builder; each node reports its output schema. |
| `physical.py` | Pull-based vectorized operators (`*Exec`): scan / filter / project / hash-aggregate (incl. global) / hash-join (inner+left, NULL keys excluded, clash-renaming) / stable multi-key sort (NULLs last both directions) / limit+offset. |
| `planner.py` | Lowers logical → physical; runs sound optimisations first: predicate pushdown into scans + top-down projection pushdown. `to_pushdown` converts filter expressions to the statistics predicate. |
| `engine.py` | `WarehouseQueryEngine` — resolves `Scan` table names against the catalog, pushes predicate/projection/snapshot down to `Table.scan`, plans, executes, concatenates. Implements `QueryEngine`. |
| `elt.py` | The watermark-based incremental ELT: `RowSource` protocol (driver-free; `ListRowSource` for tests), `ExtractSpec`, `WatermarkStore`, `EltPipeline` (paged extract → batch → load → advance watermark) with APPEND + idempotent MERGE (upsert-on-key) load modes. |

## Key design decisions

* **Determinism everywhere.** Encoders measure all candidates and pick the smallest
  (PLAIN wins ties); dictionaries are sorted; the catalog clock is injectable; the
  blob store is content-addressed. Nothing depends on iteration order or wall-clock.
* **Pushdown is conservative.** `can_skip_statistics` only returns `True` when a row
  group is *provably* empty for the predicate; the residual filter always runs, so a
  skip can never change results — only avoid work. The planner keeps the residual
  `Filter` above a scan it pushed into for exactly this reason.
* **SQL three-valued NULL logic** is implemented once in `predicate.py` /
  `expr.BoolOp` and reused by the filter operator (NULL/unknown never passes a
  filter; a definite `False` dominates AND, a definite `True` dominates OR).
* **Snapshot isolation** is optimistic: every mutation commits a new snapshot whose
  `parent_id` is the base it read; a commit against a stale base raises
  `ConcurrentCommitError`. Readers always see one consistent snapshot; time-travel
  reads any prior snapshot by id or timestamp.
* **No heavy deps.** The file format, varints, bitmaps and hashing are hand-rolled
  on the stdlib (`struct`, `hashlib`). `numpy` is available but not required.

## Contracts for sibling facets

`contracts.py` exposes:

* `Table` — `name`, `schema`, `partition_spec`, `scan(columns, predicate,
  snapshot_id)`, `current_snapshot_id()`. `catalog.CatalogTable` implements it.
* `QueryEngine` — `execute(plan) -> RecordBatch`. `engine.WarehouseQueryEngine`
  implements it; build a logical plan with the `logical.Scan` fluent API.
* `TableScan` — a resolved, declarative read request a planner can hand to an engine.

A feature-store facet builds feature tables on the catalog and reads point-in-time
slices via `scan(snapshot_id=…)`; a semantic-layer facet compiles its measures into
`logical` plans and runs them through the `QueryEngine`.

## Milestones (all complete)

1. **M1 — types + encoding** (`types.py`, `encoding.py`): the type system, the
   nullable vector, and the three byte-deterministic codecs + auto-chooser.
2. **M2 — statistics + predicate** (`statistics.py`, `predicate.py`): chunk stats,
   zonemaps, and the dual-purpose pushdown predicate algebra.
3. **M3 — columnar storage** (`column_chunk.py`, `row_group.py`, `batch.py`,
   `columnar.py`): the file format with footer, group skipping, and serialisation.
4. **M4 — partitioning + catalog** (`partition.py`, `blobstore.py`, `catalog.py`,
   `contracts.py`): Iceberg-shaped snapshots, snapshot isolation, time-travel,
   rollback, expire, partition pruning, and the sibling contracts.
5. **M5 — query engine** (`expr.py`, `aggregate.py`, `logical.py`, `physical.py`,
   `planner.py`, `engine.py`): vectorized operators, the logical+physical planner
   with predicate + projection pushdown, and the catalog-backed engine.
6. **M6 — ELT** (`elt.py`): the watermark-based incremental extract/load framework
   with APPEND + idempotent MERGE.

## Shared-file changes (additive only)

**None.** This facet is entirely self-contained under `app/lakehouse/warehouse/`
(plus `tests/test_lakehouse_*.py`). It touches no shared file — no `core/config.py`,
no `db/models/__init__.py`, no `composition.py`, no `api/routes`. Wiring into the DI
container / an API route / an Alembic-backed `BlobStore` is deliberately deferred to
a follow-on so this facet stays a pure, dependency-free library other facets import.

## Test strategy & results

All tests are **pure units, no infra** (`tests/test_lakehouse_*.py`), so they run
anywhere `make test` runs:

| Suite | File | Count area |
|---|---|---|
| type system + vector | `test_lakehouse_types.py` | coercion, nulls, transforms |
| codecs | `test_lakehouse_encoding.py` | round-trips, chooser, varints |
| statistics + zonemaps | `test_lakehouse_statistics.py` | min/max, merge, zone overlap |
| predicate algebra | `test_lakehouse_predicate.py` | eval + statistics skipping |
| columnar storage | `test_lakehouse_columnar.py` | chunks, groups, file, pushdown |
| partitioning | `test_lakehouse_partition.py` | all transforms + validation |
| catalog | `test_lakehouse_catalog.py` | snapshots, time-travel, isolation |
| expressions | `test_lakehouse_expr.py` | vectorized kernels + 3-valued NULL |
| aggregates | `test_lakehouse_aggregate.py` | accumulators + result types |
| query engine | `test_lakehouse_engine.py` | filter/project/agg/sort/join/opt |
| ELT | `test_lakehouse_elt.py` | incremental + idempotent MERGE |
| contracts | `test_lakehouse_contracts.py` | protocol conformance |

`make lint` (ruff + mypy) is clean across `app/lakehouse` and the new tests
(mypy strict: full app = 0 issues, 804 files). **169 new lakehouse unit tests
pass**; the full backend suite is green (4087 passed, 671 infra-skipped).

## Remaining / future roadmap

- **Wiring (deferred, additive):** a `Container.warehouse_catalog()` builder in
  `composition.py`, an S3/MinIO-backed `BlobStore`, an Alembic table for catalog
  metadata persistence, and a `python -m app.lakehouse.warehouse.elt_worker`
  process mirroring the ingest/rollup workers.
- **A real Postgres `RowSource`** issuing keyset-paginated `SELECT … WHERE cursor >
  :wm ORDER BY cursor LIMIT :n` over the async session (the protocol is ready).
- **Engine depth:** bloom filters per chunk for equality pushdown; sort-merge join
  for large pre-sorted inputs; spill-to-blob for aggregates exceeding memory;
  parallel row-group scans; cost-based join ordering.
- **Format depth:** bit-packed + delta + frame-of-reference integer encodings; page
  compression; column-level encryption; dictionary sharing across row groups.
- **Maintenance:** compaction (merge small files), snapshot-driven blob GC after
  `expire_snapshots`, and manifest caching for very large tables.
- **A tiny SQL-ish front-end** lowering a structured request dict onto `logical`
  nodes for the semantic-layer facet.
