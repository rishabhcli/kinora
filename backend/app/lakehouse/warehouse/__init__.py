"""Lakehouse — warehouse facet (facet A).

A self-contained, deterministic mini analytical lakehouse:

* ``types`` — the logical type system (``LogicalType``, ``Field``, ``Schema``) and
  the in-memory ``ColumnVector`` / null-bitmap primitives the whole stack shares.
* ``encoding`` — physical column codecs: PLAIN, dictionary, and run-length (RLE),
  with an automatic codec chooser. Pure, byte-deterministic round-trips.
* ``statistics`` — per-chunk column statistics (min/max/null-count/distinct) and
  zonemaps used for predicate pushdown.
* ``column_chunk`` — an encoded column + its statistics (the smallest readable unit).
* ``row_group`` — a horizontal slice of a table: aligned column chunks + group stats.
* ``columnar`` — the file format: a ``ColumnarFile`` of row groups + a footer
  (schema + per-group/per-column statistics) supporting predicate pushdown.
* ``predicate`` — the pushdown predicate algebra evaluated against statistics and
  against vectors (the same algebra the query engine reuses for filters).
* ``schema`` / ``table`` — the ``Table`` contract sibling facets consume.
* ``catalog`` — an Iceberg-shaped table catalog: snapshots, snapshot isolation,
  time-travel, partition specs, and manifest tracking.
* ``elt`` — the watermark-based incremental ELT framework extracting from the
  operational Postgres (behind a ``RowSource`` protocol) into the warehouse.
* ``expr`` / ``logical`` / ``physical`` / ``planner`` / ``engine`` — the vectorized
  query engine: expressions, a logical plan, a physical plan of vectorized
  operators (scan/filter/project/aggregate/group-by/join), the planner that lowers
  one to the other, and the ``QueryEngine`` contract.

Nothing here touches infra at import time; the ELT ``RowSource`` is a protocol so
tests drive it with in-memory fixtures.
"""

from __future__ import annotations
