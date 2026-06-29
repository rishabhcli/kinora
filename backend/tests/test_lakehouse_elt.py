"""Unit tests for the watermark-based incremental ELT framework."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.lakehouse.warehouse.catalog import Catalog, CatalogTable
from app.lakehouse.warehouse.elt import (
    EltPipeline,
    ExtractSpec,
    InMemoryWatermarkStore,
    ListRowSource,
    LoadMode,
)
from app.lakehouse.warehouse.types import Field, LogicalType, Schema


def users_schema() -> Schema:
    return Schema.of(
        Field("id", LogicalType.INT64, nullable=False),
        Field("name", LogicalType.STRING),
        Field("updated", LogicalType.INT64),
    )


@dataclass
class Harness:
    pipeline: EltPipeline
    table: CatalogTable
    source: ListRowSource
    watermarks: InMemoryWatermarkStore

    def total(self) -> int:
        return sum(b.num_rows for b in self.table.scan())


def make_pipeline(
    rows: list[dict[str, Any]],
    *,
    mode: LoadMode = LoadMode.APPEND,
    page_size: int = 1000,
    merge_key: str | None = None,
    max_pages: int = 1_000_000,
) -> Harness:
    cat = Catalog()
    schema = users_schema()
    table = cat.create_table("users", schema)
    spec = ExtractSpec(
        pipeline_name="users_elt",
        source_name="pg.users",
        target_table="users",
        cursor_column="updated",
        column_map={"id": "id", "name": "name", "updated": "updated"},
        schema=schema,
        load_mode=mode,
        merge_key=merge_key,
        page_size=page_size,
        max_pages=max_pages,
    )
    src = ListRowSource(rows)
    wm = InMemoryWatermarkStore()
    return Harness(EltPipeline(spec, src, table, wm), table, src, wm)


def test_initial_load() -> None:
    rows = [
        {"id": 1, "name": "a", "updated": 10},
        {"id": 2, "name": "b", "updated": 20},
        {"id": 3, "name": "c", "updated": 30},
    ]
    h = make_pipeline(rows)
    res = h.pipeline.run()
    assert res.rows_read == 3
    assert res.rows_loaded == 3
    assert res.new_watermark == 30
    assert h.total() == 3
    assert res.made_progress


def test_rerun_no_new_rows() -> None:
    h = make_pipeline([{"id": 1, "name": "a", "updated": 10}])
    h.pipeline.run()
    res2 = h.pipeline.run()
    assert res2.rows_read == 0
    assert not res2.made_progress
    assert h.total() == 1  # no duplicate


def test_incremental_picks_up_new_rows() -> None:
    h = make_pipeline([{"id": 1, "name": "a", "updated": 10}])
    h.pipeline.run()
    h.source.add_row({"id": 2, "name": "b", "updated": 20})
    res = h.pipeline.run()
    assert res.rows_read == 1
    assert res.new_watermark == 20
    assert h.total() == 2


def test_pagination_within_one_run() -> None:
    rows = [{"id": i, "name": f"u{i}", "updated": i} for i in range(1, 11)]
    h = make_pipeline(rows, page_size=3)
    res = h.pipeline.run()
    assert res.rows_read == 10
    assert res.pages_read == 4  # 3,3,3,1
    assert h.total() == 10


def test_watermark_persists() -> None:
    h = make_pipeline([{"id": 1, "name": "a", "updated": 100}])
    h.pipeline.run()
    assert h.watermarks.get("users_elt") == 100


def test_merge_idempotent_upsert() -> None:
    rows = [
        {"id": 1, "name": "a", "updated": 10},
        {"id": 2, "name": "b", "updated": 20},
    ]
    h = make_pipeline(rows, mode=LoadMode.MERGE, merge_key="id")
    h.pipeline.run()
    assert h.total() == 2
    # Re-extract overlapping data with an updated row for id=1 and a new id=3.
    h.watermarks.set("users_elt", None)
    h.source.set_rows(
        [
            {"id": 1, "name": "A2", "updated": 50},
            {"id": 2, "name": "b", "updated": 20},
            {"id": 3, "name": "c", "updated": 60},
        ]
    )
    h.pipeline.run()
    got = sorted((r["id"], r["name"]) for b in h.table.scan() for r in b.rows())
    assert got == [(1, "A2"), (2, "b"), (3, "c")]


def test_merge_dedupes_within_batch_last_wins() -> None:
    rows = [
        {"id": 1, "name": "first", "updated": 10},
        {"id": 1, "name": "second", "updated": 20},  # later cursor wins
    ]
    h = make_pipeline(rows, mode=LoadMode.MERGE, merge_key="id")
    h.pipeline.run()
    got = [(r["id"], r["name"]) for b in h.table.scan() for r in b.rows()]
    assert got == [(1, "second")]


def test_run_to_exhaustion() -> None:
    rows = [{"id": i, "name": f"u{i}", "updated": i} for i in range(1, 8)]
    # Cap a single run to one page via max_pages so each run advances incrementally.
    h = make_pipeline(rows, page_size=2, max_pages=1)
    results = h.pipeline.run_to_exhaustion()
    # Each run loads at most 2 rows; 7 rows -> 4 progress runs + 1 final no-progress.
    progress_runs = [r for r in results if r.made_progress]
    assert sum(r.rows_read for r in progress_runs) == 7
    assert h.total() == 7
    assert not results[-1].made_progress


def test_merge_requires_key() -> None:
    with pytest.raises(ValueError):
        ExtractSpec(
            pipeline_name="x",
            source_name="s",
            target_table="t",
            cursor_column="updated",
            column_map={"id": "id", "name": "name", "updated": "updated"},
            schema=users_schema(),
            load_mode=LoadMode.MERGE,
        )


def test_column_map_completeness_enforced() -> None:
    with pytest.raises(ValueError):
        ExtractSpec(
            pipeline_name="x",
            source_name="s",
            target_table="t",
            cursor_column="updated",
            column_map={"id": "id"},  # missing name/updated
            schema=users_schema(),
        )


def test_schema_mismatch_rejected() -> None:
    cat = Catalog()
    other = Schema.of(Field("id", LogicalType.INT64))
    table = cat.create_table("users", other)
    spec = ExtractSpec(
        pipeline_name="x",
        source_name="s",
        target_table="users",
        cursor_column="updated",
        column_map={"id": "id", "name": "name", "updated": "updated"},
        schema=users_schema(),
    )
    with pytest.raises(ValueError):
        EltPipeline(spec, ListRowSource([]), table, InMemoryWatermarkStore())


def test_list_row_source_ordering() -> None:
    src = ListRowSource(
        [{"c": 3}, {"c": 1}, {"c": 2}]
    )
    page = src.fetch_after("c", None, 10)
    assert [r["c"] for r in page] == [1, 2, 3]
    after = src.fetch_after("c", 1, 10)
    assert [r["c"] for r in after] == [2, 3]
