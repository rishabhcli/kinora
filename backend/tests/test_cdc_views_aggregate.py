"""Tests for incrementally-maintained aggregate views (no infra).

The recurring assertion: after a stream of inserts/updates/deletes (including
group-key changes), the incrementally maintained aggregate equals a from-scratch
GROUP BY over the final live rows.
"""

from __future__ import annotations

from app.streaming.cdc.events import ChangeEvent, LogPosition
from app.streaming.cdc.views import (
    AggregateView,
    AvgReducer,
    CountReducer,
    DistinctCountReducer,
    MaxReducer,
    MinReducer,
    SumReducer,
)
from app.streaming.cdc.views.engine import MaterializedViewEngine


class _Pos:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> LogPosition:
        self.n += 1
        return LogPosition(self.n, 0)


def _shots_per_book() -> AggregateView:
    return AggregateView(
        name="shots_per_book",
        source="shots",
        group_by=("book_id",),
        aggregates={"shot_count": CountReducer()},
    )


def test_count_aggregate_basic() -> None:
    engine = MaterializedViewEngine()
    engine.register(_shots_per_book())
    p = _Pos()
    engine.apply(ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1"}, p()))
    engine.apply(ChangeEvent.insert("shots", {"id": "s2", "book_id": "b1"}, p()))
    engine.apply(ChangeEvent.insert("shots", {"id": "s3", "book_id": "b2"}, p()))
    rows = {r["book_id"]: r["shot_count"] for r in engine.rows("shots_per_book")}
    assert rows == {"b1": 2, "b2": 1}


def test_count_aggregate_delete_empties_group() -> None:
    engine = MaterializedViewEngine()
    engine.register(_shots_per_book())
    p = _Pos()
    engine.apply(ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1"}, p()))
    engine.apply(ChangeEvent.delete("shots", {"id": "s1", "book_id": "b1"}, p()))
    # The group disappears entirely (no zero-count phantom row).
    assert engine.rows("shots_per_book") == []
    assert engine.state("shots_per_book").is_consistent()


def test_aggregate_update_moves_group() -> None:
    engine = MaterializedViewEngine()
    engine.register(_shots_per_book())
    p = _Pos()
    engine.apply(ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1"}, p()))
    # Reassign the shot to b2 (a group-key change).
    engine.apply(ChangeEvent.update("shots", None, {"id": "s1", "book_id": "b2"}, p()))
    rows = {r["book_id"]: r["shot_count"] for r in engine.rows("shots_per_book")}
    assert rows == {"b2": 1}


def test_sum_and_avg_reducers() -> None:
    engine = MaterializedViewEngine()
    engine.register(
        AggregateView(
            name="seconds_per_book",
            source="shots",
            group_by=("book_id",),
            aggregates={"total_s": SumReducer("duration_s"), "avg_s": AvgReducer("duration_s")},
        )
    )
    p = _Pos()
    engine.apply(ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1", "duration_s": 5.0}, p()))
    engine.apply(ChangeEvent.insert("shots", {"id": "s2", "book_id": "b1", "duration_s": 3.0}, p()))
    row = engine.rows("seconds_per_book")[0]
    assert row["total_s"] == 8.0
    assert row["avg_s"] == 4.0


def test_min_max_fall_back_under_delete() -> None:
    engine = MaterializedViewEngine()
    engine.register(
        AggregateView(
            name="extremes",
            source="shots",
            group_by=("book_id",),
            aggregates={"earliest": MinReducer("page"), "latest": MaxReducer("page")},
        )
    )
    p = _Pos()
    engine.apply(ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1", "page": 5}, p()))
    engine.apply(ChangeEvent.insert("shots", {"id": "s2", "book_id": "b1", "page": 10}, p()))
    engine.apply(ChangeEvent.insert("shots", {"id": "s3", "book_id": "b1", "page": 1}, p()))
    row = engine.rows("extremes")[0]
    assert (row["earliest"], row["latest"]) == (1, 10)
    # Delete the current min (page 1) → min falls back to the next (page 5).
    engine.apply(ChangeEvent.delete("shots", {"id": "s3", "book_id": "b1", "page": 1}, p()))
    row = engine.rows("extremes")[0]
    assert row["earliest"] == 5


def test_distinct_count_reducer() -> None:
    engine = MaterializedViewEngine()
    engine.register(
        AggregateView(
            name="distinct_modes",
            source="shots",
            group_by=("book_id",),
            aggregates={"modes": DistinctCountReducer("render_mode")},
        )
    )
    p = _Pos()
    engine.apply(
        ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1", "render_mode": "wan"}, p())
    )
    engine.apply(
        ChangeEvent.insert("shots", {"id": "s2", "book_id": "b1", "render_mode": "wan"}, p())
    )
    engine.apply(
        ChangeEvent.insert("shots", {"id": "s3", "book_id": "b1", "render_mode": "kenburns"}, p())
    )
    assert engine.rows("distinct_modes")[0]["modes"] == 2
    # Removing one of the two 'wan' shots keeps the distinct count at 2.
    engine.apply(
        ChangeEvent.delete("shots", {"id": "s1", "book_id": "b1", "render_mode": "wan"}, p())
    )
    assert engine.rows("distinct_modes")[0]["modes"] == 2


def test_where_filter_excludes_noncontributing() -> None:
    engine = MaterializedViewEngine()
    engine.register(
        AggregateView(
            name="accepted_per_book",
            source="shots",
            group_by=("book_id",),
            aggregates={"n": CountReducer()},
            where=lambda r: r.get("status") == "accepted",
        )
    )
    p = _Pos()
    engine.apply(
        ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1", "status": "planned"}, p())
    )
    assert engine.rows("accepted_per_book") == []
    # Status flips to accepted → it now contributes.
    engine.apply(
        ChangeEvent.update("shots", None, {"id": "s1", "book_id": "b1", "status": "accepted"}, p())
    )
    assert engine.rows("accepted_per_book")[0]["n"] == 1
    # Flips back → contribution removed, group empties.
    engine.apply(
        ChangeEvent.update("shots", None, {"id": "s1", "book_id": "b1", "status": "failed"}, p())
    )
    assert engine.rows("accepted_per_book") == []


def test_aggregate_consistency_oracle_after_churn() -> None:
    engine = MaterializedViewEngine()
    engine.register(_shots_per_book())
    p = _Pos()
    events = [
        ChangeEvent.insert("shots", {"id": "s1", "book_id": "b1"}, p()),
        ChangeEvent.insert("shots", {"id": "s2", "book_id": "b1"}, p()),
        ChangeEvent.insert("shots", {"id": "s3", "book_id": "b2"}, p()),
        ChangeEvent.update("shots", None, {"id": "s2", "book_id": "b2"}, p()),  # move
        ChangeEvent.delete("shots", {"id": "s1", "book_id": "b1"}, p()),  # empty b1
    ]
    for e in events:
        engine.apply(e)
    live = [{"id": "s3", "book_id": "b2"}, {"id": "s2", "book_id": "b2"}]
    result = engine.verify({"shots": live})
    assert result["shots_per_book"].consistent
