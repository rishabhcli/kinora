"""DB-backed dataset-version store tests.

Run against a throwaway Postgres and SKIP cleanly when ``KINORA_TEST_DATABASE_URL``
(the isolated ``mldata_test`` :5433) is unset, mirroring ``test_llmops_store``.
The store only ``flush``es; the session fixture rolls back on teardown so each
test is isolated.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.mlplatform.datasets.pipeline import BuildConfig, DatasetPipeline
from app.mlplatform.datasets.splitting import SplitConfig, SplitRatios
from app.mlplatform.datasets.store import DatasetVersionStore
from tests.mlplatform.factories import corpus

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping ML-data DB tests"
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


def _build() -> DatasetPipeline:
    pipe = DatasetPipeline()
    pipe.build(
        "crew",
        corpus(40),
        config=BuildConfig(split=SplitConfig(ratios=SplitRatios(0.7, 0.15, 0.15))),
    )
    return pipe


async def test_persist_and_read_back(session: AsyncSession) -> None:
    pipe = _build()
    store = DatasetVersionStore(session)
    versions = pipe.registry.history("crew")
    for v in versions:
        await store.persist_version(v)

    final = versions[-1]
    row = await store.get_version_row(final.version_id)
    assert row is not None
    assert row.n_examples == final.n
    assert row.operation == "split"

    latest = await store.latest_version_id("crew")
    assert latest == final.version_id
    assert await store.names() == ["crew"]


async def test_persist_is_idempotent(session: AsyncSession) -> None:
    pipe = _build()
    store = DatasetVersionStore(session)
    final = pipe.registry.latest("crew")
    assert await store.persist_version(final) is True
    assert await store.persist_version(final) is False  # no-op second time


async def test_example_records_by_split(session: AsyncSession) -> None:
    pipe = _build()
    store = DatasetVersionStore(session)
    final = pipe.registry.latest("crew")
    await store.persist_version(final)
    train = await store.example_records(final.version_id, split="train")
    assert all(r["split"] == "train" for r in train)
    assert 0 < len(train) <= final.n


async def test_lineage_ancestry(session: AsyncSession) -> None:
    pipe = _build()
    store = DatasetVersionStore(session)
    for v in pipe.registry.history("crew"):
        await store.persist_version(v)
    final = pipe.registry.latest("crew")
    ancestry = await store.lineage_ancestry(final.version_id)
    # ingest → scrub → dedup → label → split == 5 nodes
    assert len(ancestry) == 5
    assert final.version_id in ancestry
