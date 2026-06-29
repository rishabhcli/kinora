"""Offline store + training-set generation (point-in-time-correct joins)."""

from __future__ import annotations

import pytest

from app.lakehouse.features import (
    Entity,
    FeatureRegistry,
    FeatureSource,
    FeatureSpec,
    FeatureView,
    InMemoryOfflineStore,
    PointInTimeError,
    ValueType,
    get_historical_features,
    point_in_time_join,
)
from app.lakehouse.features.offline_store import latest_rows
from app.lakehouse.features.rows import EntityRow

from .conftest import at, book_features_view, book_row, user_row, user_stats_view


def _registry() -> FeatureRegistry:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view(ttl_minutes=60))
    reg.register_feature_view(book_features_view())
    return reg


def test_idempotent_append() -> None:
    store = InMemoryOfflineStore()
    row = user_row("u1", minute=0, pages=1, dwell=2.0, genre="scifi")
    assert store.write("user_stats", [row]) == 1
    assert store.write("user_stats", [row]) == 0  # duplicate ignored
    assert store.row_count("user_stats") == 1


def test_training_set_picks_value_known_at_label_time() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    # Two observations for u1: at minute 10 and minute 30.
    store.write(
        "user_stats",
        [
            user_row("u1", minute=10, pages=5, dwell=20.0, genre="scifi"),
            user_row("u1", minute=30, pages=12, dwell=40.0, genre="fantasy"),
        ],
    )
    # Label observed at minute 25 → must see the minute-10 value, NOT minute 30.
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=at(25))],
        refs=["user_stats:pages_read", "user_stats:genre"],
    )
    rows = frame.to_dicts()
    assert len(rows) == 1
    assert rows[0]["user_stats__pages_read"] == 5
    assert rows[0]["user_stats__genre"] == "scifi"


def test_training_set_respects_ttl() -> None:
    reg = _registry()  # user_stats ttl = 60 minutes
    store = InMemoryOfflineStore()
    store.write("user_stats", [user_row("u1", minute=0, pages=5, dwell=20.0, genre="scifi")])
    # Label at minute 90 → the only value (minute 0) is 90 min old > 60 ttl → default.
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=at(90))],
        refs=["user_stats:pages_read"],
    )
    assert frame.to_dicts()[0]["user_stats__pages_read"] == 0  # the default


def test_training_set_default_when_no_history() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "ghost"}, event_timestamp=at(5))],
        refs=["user_stats:pages_read", "user_stats:avg_dwell_s"],
    )
    row = frame.to_dicts()[0]
    assert row["user_stats__pages_read"] == 0
    assert row["user_stats__avg_dwell_s"] == 0.0


def test_multi_entity_multi_view_join() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    store.write("user_stats", [user_row("u1", minute=5, pages=3, dwell=10.0, genre="scifi")])
    store.write("book_feats", [book_row("b1", minute=2, popularity=0.9, embedding=[1.0, 2.0])])
    frame = get_historical_features(
        store,
        reg,
        entities=[
            EntityRow(keys={"user_id": "u1", "book_id": "b1"}, event_timestamp=at(10)),
        ],
        refs=["user_stats:pages_read", "book_feats:popularity"],
    )
    row = frame.to_dicts()[0]
    assert row["user_stats__pages_read"] == 3
    assert row["book_feats__popularity"] == 0.9
    assert row["user_id"] == "u1" and row["book_id"] == "b1"


def test_tie_break_prefers_latest_arrival() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    # Two rows at the SAME event time (minute 10) but different arrival times.
    store.write(
        "user_stats",
        [
            user_row("u1", minute=10, pages=1, dwell=1.0, genre="a", created=10),
            user_row("u1", minute=10, pages=99, dwell=9.0, genre="b", created=20),  # later arrival
        ],
    )
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=at(15))],
        refs=["user_stats:pages_read"],
    )
    assert frame.to_dicts()[0]["user_stats__pages_read"] == 99  # latest arrival wins


def test_full_feature_names_false_uses_bare_names() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    store.write("user_stats", [user_row("u1", minute=1, pages=7, dwell=3.0, genre="x")])
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=at(5))],
        refs=["user_stats:pages_read"],
        full_feature_names=False,
    )
    assert "pages_read" in frame.columns
    assert frame.to_dicts()[0]["pages_read"] == 7


def test_column_order_follows_reference_order() -> None:
    reg = _registry()
    store = InMemoryOfflineStore()
    store.write("user_stats", [user_row("u1", minute=1, pages=7, dwell=3.0, genre="x")])
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=at(5))],
        refs=["user_stats:avg_dwell_s", "user_stats:pages_read"],
    )
    # event_timestamp + the two features in reference order.
    assert frame.columns[-2:] == ("user_stats__avg_dwell_s", "user_stats__pages_read")


def test_latest_rows_for_materialization() -> None:
    view = user_stats_view(ttl_minutes=60)
    store = InMemoryOfflineStore()
    store.write(
        "user_stats",
        [
            user_row("u1", minute=10, pages=1, dwell=1.0, genre="a"),
            user_row("u1", minute=40, pages=2, dwell=2.0, genre="b"),
            user_row("u2", minute=5, pages=9, dwell=9.0, genre="c"),
        ],
    )
    latest = latest_rows(store, view, as_of=at(50))
    assert latest[("u1",)].values["pages_read"] == 2  # newest within ttl
    assert latest[("u2",)].values["pages_read"] == 9


def test_latest_rows_excludes_stale() -> None:
    view = user_stats_view(ttl_minutes=30)
    store = InMemoryOfflineStore()
    store.write("user_stats", [user_row("u1", minute=0, pages=1, dwell=1.0, genre="a")])
    # as_of minute 100, only value is minute 0 → stale (>30) → excluded.
    latest = latest_rows(store, view, as_of=at(100))
    assert ("u1",) not in latest


def test_point_in_time_join_collision_without_namespacing_raises() -> None:
    # Two views with a same-named feature collide when full_feature_names=False.
    v1 = FeatureView(
        name="a",
        entities=(Entity(name="user"),),
        features=(FeatureSpec(name="x", dtype=ValueType.INT),),
        source=FeatureSource(name="sa"),
    )
    v2 = FeatureView(
        name="b",
        entities=(Entity(name="user"),),
        features=(FeatureSpec(name="x", dtype=ValueType.INT),),
        source=FeatureSource(name="sb"),
    )
    with pytest.raises(PointInTimeError):
        point_in_time_join(
            [EntityRow(keys={"user_id": "u1"}, event_timestamp=at(1))],
            views=[v1, v2],
            sources={},
            full_feature_names=False,
        )
