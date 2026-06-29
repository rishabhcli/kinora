"""Online store + offline→online materialisation (latest-value serving)."""

from __future__ import annotations

import pytest

from app.lakehouse.features import (
    FeatureRegistry,
    InMemoryOfflineStore,
    InMemoryOnlineStore,
    OnlineValue,
    get_online_features,
    materialize,
)
from app.lakehouse.features.materialization import materialize_view
from app.lakehouse.features.online_store import serialize_value

from .conftest import at, book_features_view, book_row, user_row, user_stats_view

pytestmark = pytest.mark.asyncio


def _registry() -> FeatureRegistry:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view(ttl_minutes=60))
    reg.register_feature_view(book_features_view())
    return reg


async def test_materialize_pushes_latest_value() -> None:
    reg = _registry()
    offline = InMemoryOfflineStore()
    online = InMemoryOnlineStore()
    offline.write(
        "user_stats",
        [
            user_row("u1", minute=10, pages=1, dwell=1.0, genre="a"),
            user_row("u1", minute=40, pages=5, dwell=5.0, genre="b"),
        ],
    )
    results = await materialize(reg, offline=offline, online=online, as_of=at(50))
    user_result = next(r for r in results if r.view == "user_stats")
    assert user_result.rows_written == 1
    view = reg.get_feature_view("user_stats")
    value = await online.get(view, ("u1",))
    assert value is not None
    assert value.values["pages_read"] == 5  # the newest within ttl


async def test_materialize_skips_stale() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view(ttl_minutes=30))
    offline = InMemoryOfflineStore()
    online = InMemoryOnlineStore()
    offline.write("user_stats", [user_row("u1", minute=0, pages=1, dwell=1.0, genre="a")])
    result = await materialize_view(
        reg.get_feature_view("user_stats"), offline=offline, online=online, as_of=at(100)
    )
    assert result.rows_written == 0
    assert result.keys_total == 1
    assert result.coverage == 0.0


async def test_get_online_features_fills_defaults() -> None:
    reg = _registry()
    online = InMemoryOnlineStore()
    view = reg.get_feature_view("user_stats")
    # No materialised value for this key → all defaults.
    vector = await get_online_features(online, views=[view], keys={"user_id": "ghost"})
    assert vector["user_stats__pages_read"] == 0
    assert vector["user_stats__genre"] == "unknown"


async def test_get_online_features_serves_materialized() -> None:
    reg = _registry()
    offline = InMemoryOfflineStore()
    online = InMemoryOnlineStore()
    offline.write("user_stats", [user_row("u1", minute=10, pages=7, dwell=3.0, genre="scifi")])
    await materialize(reg, offline=offline, online=online, as_of=at(20))
    view = reg.get_feature_view("user_stats")
    vector = await get_online_features(online, views=[view], keys={"user_id": "u1"})
    assert vector["user_stats__pages_read"] == 7
    assert vector["user_stats__genre"] == "scifi"


async def test_materialize_only_online_views_by_default() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    # An offline-only view (online=False) is skipped by materialize(views=None).
    from app.lakehouse.features import Entity, FeatureSource, FeatureSpec, FeatureView, ValueType

    offline_only = FeatureView(
        name="train_only",
        entities=(Entity(name="user"),),
        features=(FeatureSpec(name="label", dtype=ValueType.FLOAT),),
        source=FeatureSource(name="t"),
        online=False,
    )
    reg.register_feature_view(offline_only)
    offline = InMemoryOfflineStore()
    online = InMemoryOnlineStore()
    results = await materialize(reg, offline=offline, online=online, as_of=at(1))
    assert {r.view for r in results} == {"user_stats"}


async def test_embedding_vector_round_trips_online() -> None:
    reg = _registry()
    offline = InMemoryOfflineStore()
    online = InMemoryOnlineStore()
    offline.write(
        "book_feats", [book_row("b1", minute=1, popularity=0.5, embedding=[0.1, 0.2, 0.3])]
    )
    await materialize(reg, offline=offline, online=online, as_of=at(5))
    view = reg.get_feature_view("book_feats")
    vector = await get_online_features(online, views=[view], keys={"book_id": "b1"})
    assert vector["book_feats__embedding"] == [0.1, 0.2, 0.3]


async def test_serialize_value_is_json() -> None:
    blob = serialize_value(OnlineValue(values={"x": 1}, event_timestamp=at(0)))
    assert '"x":1' in blob
    assert "2026-01-01" in blob
