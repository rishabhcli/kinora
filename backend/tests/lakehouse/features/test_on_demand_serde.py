"""On-demand/streaming computation seam + definition (de)serialisation."""

from __future__ import annotations

import pytest

from app.lakehouse.features import (
    FeatureRegistry,
    InMemoryOnlineStore,
    OnDemandFeatureView,
    apply_on_demand,
    days_since,
    get_online_features,
    push_stream_rows,
)
from app.lakehouse.features.serde import feature_view_from_dict, feature_view_to_dict
from app.lakehouse.features.types import FeatureSpec, ValueType

from .conftest import at, book_features_view, user_row, user_stats_view

# asyncio_mode = "auto" (pyproject) collects async tests automatically; no marker
# needed, and a module-level asyncio mark would (wrongly) flag the sync tests here.


# --------------------------------------------------------------------------- #
# On-demand
# --------------------------------------------------------------------------- #


async def test_apply_on_demand_augments_base() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    odv = OnDemandFeatureView(
        name="ratio",
        features=(FeatureSpec(name="dwell_per_page", dtype=ValueType.FLOAT, default=0.0),),
        source_views=("user_stats",),
    )

    def fn(request, upstream):  # type: ignore[no-untyped-def]
        pages = upstream.get("pages_read") or 1
        dwell = upstream.get("avg_dwell_s") or 0.0
        return {"dwell_per_page": dwell / pages}

    reg.register_on_demand_view(odv, fn)
    base = {"user_stats__pages_read": 4, "user_stats__avg_dwell_s": 20.0}
    out = apply_on_demand(reg, base=base, on_demand_views=["ratio"], request={})
    assert out["ratio__dwell_per_page"] == 5.0
    assert out["user_stats__pages_read"] == 4  # base preserved


async def test_on_demand_reads_request_inputs() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    odv = OnDemandFeatureView(
        name="recency",
        features=(FeatureSpec(name="days_idle", dtype=ValueType.FLOAT, default=0.0),),
        request_inputs=("now",),
    )

    def fn(request, upstream):  # type: ignore[no-untyped-def]
        return {"days_idle": float(request.get("now", 0))}

    reg.register_on_demand_view(odv, fn)
    out = apply_on_demand(reg, base={}, on_demand_views=["recency"], request={"now": 3.0})
    assert out["recency__days_idle"] == 3.0


def test_days_since_helper() -> None:
    assert days_since(None, now=at(100)) is None
    assert days_since(at(0), now=at(60 * 24)) == pytest.approx(1.0)  # 1440 min = 1 day
    assert days_since("2026-01-01T00:00:00+00:00", now=at(60 * 24 * 2)) == pytest.approx(2.0)
    assert days_since("not-a-date", now=at(0)) is None


async def test_push_stream_rows_serves_immediately() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    view = reg.get_feature_view("user_stats")
    online = InMemoryOnlineStore()
    # Micro-batch with two events for u1; the latest event time wins.
    n = await push_stream_rows(
        view,
        [
            user_row("u1", minute=10, pages=1, dwell=1.0, genre="a"),
            user_row("u1", minute=20, pages=5, dwell=5.0, genre="b"),
        ],
        online=online,
    )
    assert n == 1
    vector = await get_online_features(online, views=[view], keys={"user_id": "u1"})
    assert vector["user_stats__pages_read"] == 5  # newest streamed event


# --------------------------------------------------------------------------- #
# Serde round-trip
# --------------------------------------------------------------------------- #


def test_feature_view_serde_round_trip() -> None:
    original = user_stats_view(ttl_minutes=90)
    reg = FeatureRegistry()
    stamped = reg.register_feature_view(original)  # has a version
    blob = feature_view_to_dict(stamped)
    restored = feature_view_from_dict(blob)
    assert restored.name == stamped.name
    assert restored.version == stamped.version
    assert restored.ttl == stamped.ttl
    assert restored.feature_names == stamped.feature_names
    assert restored.join_keys == stamped.join_keys
    # Re-registering the restored view reproduces the same content-addressed version.
    reg2 = FeatureRegistry()
    assert reg2.register_feature_view(restored).version == stamped.version


def test_feature_view_serde_preserves_source_and_defaults() -> None:
    view = book_features_view(ttl_minutes=30)
    blob = feature_view_to_dict(view)
    restored = feature_view_from_dict(blob)
    assert restored.source.name == "book_feats_src"
    assert restored.feature("embedding").default is None
    assert restored.ttl is not None and restored.ttl.total_seconds() == 30 * 60
