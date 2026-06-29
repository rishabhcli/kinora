"""Postgres-backed feature-store spine (durable offline history + registry snapshot).

Runs against a throwaway Postgres and SKIPs cleanly when
``KINORA_TEST_DATABASE_URL`` is unset (isolated DB ``featstore_test`` on :5433, per
the marathon convention). Each test isolates by rolling back on teardown — the
repos only flush, never commit.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.lakehouse.features import (
    FeatureRegistry,
    InMemoryOnlineStore,
    get_historical_features,
    materialize,
)
from app.lakehouse.features.db_repo import (
    DbOfflineStore,
    FeatureMaterializationRepo,
    FeatureOfflineRepo,
    FeatureViewDefRepo,
    entity_key_str,
)
from app.lakehouse.features.materialization import MaterializationResult
from app.lakehouse.features.rows import EntityRow, FeatureRow

from .conftest import user_stats_view

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping feature-store DB tests"
)

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _row(uid: str, *, minute: float, pages: int) -> FeatureRow:
    return FeatureRow(
        keys={"user_id": uid},
        values={"pages_read": pages, "avg_dwell_s": 1.0, "genre": "x"},
        event_timestamp=_at(minute),
        created_timestamp=_at(minute),
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


def test_entity_key_str_is_stable() -> None:
    assert entity_key_str({"user_id": "u1"}, ["user_id"]) == "u1"
    assert entity_key_str({"a": 1, "b": 2}, ["a", "b"]) == "1\x1f2"
    assert entity_key_str({"a": None}, ["a"]) == ""


async def test_offline_append_is_idempotent(session: AsyncSession) -> None:
    view = user_stats_view()
    repo = FeatureOfflineRepo(session)
    rows = [_row("u1", minute=0, pages=1), _row("u1", minute=10, pages=2)]
    assert await repo.append(view, rows) == 2
    # Re-appending the same identities adds nothing.
    assert await repo.append(view, rows) == 0
    loaded = await repo.load(view)
    assert len(loaded) == 2


async def test_offline_load_scoped_to_entity_keys(session: AsyncSession) -> None:
    view = user_stats_view()
    repo = FeatureOfflineRepo(session)
    await repo.append(view, [_row("u1", minute=0, pages=1), _row("u2", minute=0, pages=9)])
    scoped = await repo.load(view, entity_keys=["u1"])
    assert {r.keys["user_id"] for r in scoped} == {"u1"}


async def test_db_offline_store_training_join(session: AsyncSession) -> None:
    view = user_stats_view(ttl_minutes=60)
    reg = FeatureRegistry()
    reg.register_feature_view(view)
    store = DbOfflineStore(session)
    await store.persist(view, [_row("u1", minute=10, pages=5), _row("u1", minute=40, pages=12)])
    await store.hydrate([view])
    frame = get_historical_features(
        store,
        reg,
        entities=[EntityRow(keys={"user_id": "u1"}, event_timestamp=_at(25))],
        refs=["user_stats:pages_read"],
    )
    # Label at minute 25 → the minute-10 value (no leakage of minute 40).
    assert frame.to_dicts()[0]["user_stats__pages_read"] == 5


async def test_db_offline_store_materializes_to_online(session: AsyncSession) -> None:
    view = user_stats_view(ttl_minutes=120)
    reg = FeatureRegistry()
    reg.register_feature_view(view)
    store = DbOfflineStore(session)
    await store.persist(view, [_row("u1", minute=10, pages=5), _row("u1", minute=40, pages=12)])
    await store.hydrate([view])
    online = InMemoryOnlineStore()
    results = await materialize(reg, offline=store, online=online, as_of=_at(60))
    assert results[0].rows_written == 1
    value = await online.get(reg.get_feature_view("user_stats"), ("u1",))
    assert value is not None and value.values["pages_read"] == 12


async def test_view_def_snapshot_round_trip(session: AsyncSession) -> None:
    reg = FeatureRegistry()
    stamped = reg.register_feature_view(user_stats_view(ttl_minutes=90))
    repo = FeatureViewDefRepo(session)
    await repo.upsert(stamped)
    # Idempotent upsert (same content-addressed version).
    await repo.upsert(stamped)
    fresh_registry = FeatureRegistry()
    count = await repo.rehydrate_into(fresh_registry)
    assert count == 1
    rehydrated = fresh_registry.get_feature_view("user_stats")
    assert rehydrated.version == stamped.version
    assert rehydrated.ttl == timedelta(minutes=90)


async def test_materialization_run_logged(session: AsyncSession) -> None:
    repo = FeatureMaterializationRepo(session)
    await repo.record(
        MaterializationResult(
            view="user_stats", version=7, as_of=_at(0), rows_written=3, keys_total=5
        )
    )
    from sqlalchemy import func, select

    from app.lakehouse.features.db_models import FeatureMaterialization

    count = await session.scalar(
        select(func.count()).select_from(FeatureMaterialization)
    )
    assert count == 1
