"""Offset-store tests: in-memory (unit) + DB-backed (infra-gated)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

from app.streaming.cdc.events import LogPosition
from app.streaming.cdc.offsets import DbOffsetStore, InMemoryOffsetStore

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")


async def test_in_memory_offset_store_monotonic() -> None:
    store = InMemoryOffsetStore()
    assert await store.load("lib", "books") == LogPosition.zero()
    await store.commit("lib", "books", LogPosition(10, 0))
    assert await store.load("lib", "books") == LogPosition(10, 0)
    # A late, lower commit cannot rewind the offset.
    await store.commit("lib", "books", LogPosition(5, 0))
    assert await store.load("lib", "books") == LogPosition(10, 0)
    # Distinct connector/table tracked separately.
    await store.commit("other", "books", LogPosition(3, 0))
    assert await store.load("other", "books") == LogPosition(3, 0)
    assert len(store.snapshot()) == 2


@pytest.mark.skipif(_DB_URL is None, reason="needs KINORA_TEST_DATABASE_URL")
async def test_db_offset_store_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import models  # noqa: F401  (register tables)
    from app.db.base import Base

    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        async with maker() as session:
            yield session
            await session.commit()

    store = DbOffsetStore(scope)
    assert await store.load("cdc_test", "books") == LogPosition.zero()
    await store.commit("cdc_test", "books", LogPosition(42, 1))
    assert await store.load("cdc_test", "books") == LogPosition(42, 1)
    await store.commit("cdc_test", "books", LogPosition(40, 0))  # no rewind
    assert await store.load("cdc_test", "books") == LogPosition(42, 1)

    async with engine.begin() as conn:
        from app.streaming.cdc.models import CdcOffset

        await conn.run_sync(CdcOffset.__table__.drop)  # type: ignore[attr-defined]
    await engine.dispose()
