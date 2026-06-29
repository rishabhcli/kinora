"""End-to-end tests for the :class:`FeatureStore` façade (define→ingest→serve)."""

from __future__ import annotations

import pytest

from app.lakehouse.features import (
    FeatureStore,
    OnDemandFeatureView,
)
from app.lakehouse.features.types import FeatureSpec, ValueType

from .conftest import at, book_row, user_row, user_stats_view


@pytest.mark.asyncio
async def test_end_to_end_training_then_serving(store: FeatureStore) -> None:
    # Ingest offline history.
    store.ingest("user_stats", [user_row("u1", minute=10, pages=5, dwell=20.0, genre="scifi")])
    store.ingest("user_stats", [user_row("u1", minute=40, pages=12, dwell=40.0, genre="fantasy")])
    store.ingest("book_feats", [book_row("b1", minute=5, popularity=0.8, embedding=[1.0])])

    # Training set via the feature service (point-in-time correct).
    frame = store.get_training_data(entities=_entities(), service="recsys_v1")
    rows = frame.to_dicts()
    by_user = {r["user_id"]: r for r in rows}
    # u1 label at minute 25 → sees minute-10 value (pages=5), not the future 40.
    assert by_user["u1"]["user_stats__pages_read"] == 5
    assert by_user["u1"]["book_feats__popularity"] == 0.8

    # Materialise + serve the current vector.
    await store.materialize(as_of=at(50))
    vector = await store.get_online_features(
        keys={"user_id": "u1", "book_id": "b1"}, service="recsys_v1"
    )
    assert vector["user_stats__pages_read"] == 12  # the latest
    assert vector["book_feats__popularity"] == 0.8


def _entities():  # type: ignore[no-untyped-def]
    from app.lakehouse.features.rows import EntityRow

    return [
        EntityRow(keys={"user_id": "u1", "book_id": "b1"}, event_timestamp=at(25)),
    ]


@pytest.mark.asyncio
async def test_store_serving_records_monitor(store: FeatureStore) -> None:
    store.ingest("user_stats", [user_row("u1", minute=1, pages=3, dwell=1.0, genre="x")])
    await store.materialize(as_of=at(5))
    await store.get_online_features(keys={"user_id": "u1"}, refs=["user_stats:pages_read"])
    await store.get_online_features(keys={"user_id": "ghost"}, refs=["user_stats:pages_read"])
    snap = store.monitor.snapshot()
    assert snap.counters["online_reads"] == 2
    assert snap.counters["online_hits"] == 1
    assert snap.counters["online_misses"] == 1


@pytest.mark.asyncio
async def test_store_on_demand_in_serving(store: FeatureStore) -> None:
    odv = OnDemandFeatureView(
        name="ratio",
        features=(FeatureSpec(name="dwell_per_page", dtype=ValueType.FLOAT, default=0.0),),
        source_views=("user_stats",),
    )

    def fn(request, upstream):  # type: ignore[no-untyped-def]
        pages = upstream.get("pages_read") or 1
        return {"dwell_per_page": (upstream.get("avg_dwell_s") or 0.0) / pages}

    store.register_on_demand_view(odv, fn)
    store.ingest("user_stats", [user_row("u1", minute=1, pages=4, dwell=20.0, genre="x")])
    await store.materialize(as_of=at(5))
    vector = await store.get_online_features(
        keys={"user_id": "u1"},
        refs=["user_stats:avg_dwell_s"],
        on_demand_views=["ratio"],
    )
    assert vector["ratio__dwell_per_page"] == 5.0


@pytest.mark.asyncio
async def test_store_push_streaming(store: FeatureStore) -> None:
    n = await store.push("user_stats", [user_row("u1", minute=30, pages=9, dwell=3.0, genre="x")])
    assert n == 1
    vector = await store.get_online_features(keys={"user_id": "u1"}, refs=["user_stats:pages_read"])
    assert vector["user_stats__pages_read"] == 9


def test_store_validate_parity_and_skew(store: FeatureStore) -> None:
    offline = {"u1": {"pages_read": 5, "avg_dwell_s": 1.0, "genre": "a"}}
    online = {"u1": {"pages_read": 5, "avg_dwell_s": 1.0, "genre": "a"}}
    report = store.validate_parity("user_stats", offline=offline, online=online)
    assert report.ok
    assert store.monitor.snapshot().view_health["user_stats"].parity_match_rate == 1.0

    skew = store.detect_skew(
        "user_stats",
        reference={"pages_read": [1, 2, 3, 4, 5]},
        current={"pages_read": [1, 2, 3, 4, 5]},
    )
    assert skew.ok


def test_store_assess_freshness(store: FeatureStore) -> None:
    report = store.assess_freshness(
        "user_stats", event_timestamps=[at(95), at(10)], now=at(100)
    )
    assert report.total == 2


def test_store_requires_service_or_refs(store: FeatureStore) -> None:
    with pytest.raises(ValueError):
        store.get_training_data(entities=_entities())
    with pytest.raises(ValueError):
        store.get_training_data(
            entities=_entities(), refs=["user_stats:pages_read"], service="recsys_v1"
        )


def test_store_ingest_unknown_view_raises(store: FeatureStore) -> None:
    from app.lakehouse.features import ReferenceError

    with pytest.raises(ReferenceError):
        store.ingest("nonexistent", [])


def test_default_store_is_infra_free() -> None:
    # No redis client / engine → in-memory backends, constructs without infra.
    fs = FeatureStore()
    fs.register_feature_view(user_stats_view())
    from app.lakehouse.features import InMemoryOfflineStore, InMemoryOnlineStore

    assert isinstance(fs.offline, InMemoryOfflineStore)
    assert isinstance(fs.online, InMemoryOnlineStore)
