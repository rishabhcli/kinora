"""Integration tests for the read/write split routing factory.

Single-node throwaway DB on :5433 (no real replica): the test asserts that
``read()`` and ``write()`` both commit correctly and that, with no replica, the
reader maker is the *same* factory as the writer maker (the safe fallback).
SKIPs when ``KINORA_TEST_DATABASE_URL`` is unset.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Integer, String, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import Settings
from app.db.engine import EngineRegistry
from app.db.routing import RoutingSessionFactory

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)


class _Base(DeclarativeBase):
    pass


class Note(_Base):
    __tablename__ = "test_routing_notes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(String(256))
    n: Mapped[int] = mapped_column(Integer, default=0)


@pytest_asyncio.fixture
async def routing() -> AsyncIterator[RoutingSessionFactory]:
    assert _DB_URL is not None
    settings = Settings(dashscope_api_key="test", database_url=_DB_URL)
    registry = EngineRegistry.from_settings(settings)
    async with registry.writer().begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    try:
        yield RoutingSessionFactory.from_registry(registry)
    finally:
        async with registry.writer().begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await registry.dispose()


async def test_write_commits_and_read_sees_it(routing: RoutingSessionFactory) -> None:
    async with routing.write() as db:
        db.add(Note(id="n1", body="hello", n=1))

    # A fresh read transaction sees the committed write.
    async with routing.read() as db:
        rows = (await db.execute(select(Note))).scalars().all()
    assert [r.id for r in rows] == ["n1"]


async def test_no_replica_reader_is_writer_maker(routing: RoutingSessionFactory) -> None:
    # Single-node: the reader maker IS the writer maker (no second pool).
    assert routing.reader_maker() is routing.writer_maker()
    assert routing.registry.has_replica is False


async def test_session_by_intent(routing: RoutingSessionFactory) -> None:
    async with routing.session(readonly=False) as db:
        db.add(Note(id="n2", body="write-intent", n=2))
    async with routing.session(readonly=True) as db:
        row = await db.get(Note, "n2")
        assert row is not None and row.body == "write-intent"


async def test_write_rolls_back_on_error(routing: RoutingSessionFactory) -> None:
    with pytest.raises(RuntimeError, match="kaboom"):
        async with routing.write() as db:
            db.add(Note(id="n3", body="doomed", n=3))
            await db.flush()
            raise RuntimeError("kaboom")
    async with routing.read() as db:
        assert await db.get(Note, "n3") is None


async def test_read_executes_plain_sql(routing: RoutingSessionFactory) -> None:
    async with routing.read() as db:
        assert (await db.execute(text("SELECT 7"))).scalar_one() == 7
