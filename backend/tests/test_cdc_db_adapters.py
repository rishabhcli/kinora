"""Infra-gated tests for the SQLAlchemy CDC adapters.

These require a Postgres test DB (``KINORA_TEST_DATABASE_URL``). They exercise
the polling RowFetcher over the real ``books`` table and the view-state
checkpoint store roundtrip. They create + drop their own rows/tables and never
touch live data.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

from app.streaming.cdc.db_adapters import SqlAlchemyRowFetcher, ViewStateCheckpointStore
from app.streaming.cdc.polling_source import PollingSource
from app.streaming.cdc.views import LibraryShelfView
from app.streaming.cdc.views.delta import Row

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(_DB_URL is None, reason="needs KINORA_TEST_DATABASE_URL")


@asynccontextmanager
async def _engine_and_scope():  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import models  # noqa: F401
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

    try:
        yield engine, scope
    finally:
        await engine.dispose()


async def test_sqlalchemy_row_fetcher_snapshot_over_books() -> None:
    from app.db.base import new_id
    from app.db.models.book import Book
    from app.db.models.enums import BookStatus

    async with _engine_and_scope() as (engine, scope):
        from sqlalchemy import delete

        marker = f"cdc-adapter-{new_id()[:8]}"
        async with scope() as session:
            session.add(Book(title=marker, status=BookStatus.READY))

        fetcher = SqlAlchemyRowFetcher(scope, Book)
        snap = await fetcher.fetch_snapshot("books", limit=10_000)
        titles = {r["title"] for r in snap}
        assert marker in titles
        # Each row carries the projected cursor column.
        assert all("__updated_at_micros" in r for r in snap)

        # Drive the polling source's snapshot off the fetcher (smoke).
        src = PollingSource(fetcher, ["books"])
        events = [e async for e in src.snapshot()]
        assert any(e.after and e.after.get("title") == marker for e in events)

        # Cleanup our row.
        async with scope() as session:
            await session.execute(delete(Book).where(Book.title == marker))


async def test_view_state_checkpoint_roundtrip() -> None:
    async with _engine_and_scope() as (engine, scope):
        from sqlalchemy import delete

        from app.streaming.cdc.models import CdcViewStateRow

        view = LibraryShelfView()
        view.state.add(Row({"book_id": "b1", "title": "Dune"}), +1)
        view.state.add(Row({"book_id": "b2", "title": "Messiah"}), +1)

        store = ViewStateCheckpointStore(scope)
        written = await store.save(view)
        assert written == 2

        rehydrated = await store.load("library_shelf")
        assert len(rehydrated.rows()) == 2
        rows = await store.rows("library_shelf")
        assert {r["book_id"] for r in rows} == {"b1", "b2"}

        async with scope() as session:
            await session.execute(
                delete(CdcViewStateRow).where(CdcViewStateRow.view_name == "library_shelf")
            )
