"""Postgres integration tests for the generic repository, mixins, and UoW.

These define their *own* throwaway tables on a dedicated metadata (so they never
touch the app's ``Base.metadata``) and run against ``kinora_dblayer_test`` on
:5433. They SKIP cleanly when ``KINORA_TEST_DATABASE_URL`` is unset.

Covers:
* generic CRUD + filtering + ordering + offset/keyset pagination,
* soft-delete-aware reads + restore (``SoftDeleteMixin``),
* optimistic-concurrency conflict raising ``StaleDataError`` (``VersionedMixin``),
* the unit-of-work savepoint + repository registry,
* retry-on-serialization-failure via :func:`run_in_uow`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import NullPool

from app.db.mixins import AuditMixin, SoftDeleteMixin, VersionedMixin
from app.db.query import Cursor
from app.db.repositories.generic import GenericRepository
from app.db.retry import RetryClass, RetryPolicy
from app.db.unit_of_work import UnitOfWork, run_in_uow, unit_of_work

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)


class _Base(DeclarativeBase):
    pass


class Gadget(SoftDeleteMixin, AuditMixin, _Base):
    """A soft-deletable, audited test row."""

    __tablename__ = "test_gadgets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    rank: Mapped[int] = mapped_column(Integer, default=0)


class Counter(VersionedMixin, _Base):
    """A versioned (optimistic-concurrency) test row."""

    __tablename__ = "test_counters"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class GadgetRepo(GenericRepository[Gadget, str]):
    model = Gadget


class CounterRepo(GenericRepository[Counter, str]):
    model = Counter


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    db = maker()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()


# --- generic CRUD -----------------------------------------------------------


async def test_create_get_update_delete(session: AsyncSession) -> None:
    repo = GadgetRepo(session)
    g = await repo.create(id="g1", name="alpha", rank=3, created_by="user_1")
    assert g.id == "g1"
    assert g.created_by == "user_1"

    fetched = await repo.get("g1")
    assert fetched is not None and fetched.name == "alpha"

    await repo.update("g1", name="alpha-2", rank=9)
    await session.refresh(g)
    assert g.name == "alpha-2" and g.rank == 9

    # get_or_raise on a missing id
    with pytest.raises(KeyError):
        await repo.get_or_raise("missing")

    assert await repo.exists(name="alpha-2") is True
    assert await repo.exists(name="nope") is False
    assert await repo.count() == 1


async def test_list_filter_order_paginate(session: AsyncSession) -> None:
    repo = GadgetRepo(session)
    for i in range(10):
        await repo.create(id=f"g{i:02d}", name="x" if i % 2 else "y", rank=i)

    high = await repo.list_rows(filters={"rank__ge": 5}, order_by=["-rank"])
    assert [g.rank for g in high] == [9, 8, 7, 6, 5]

    only_x = await repo.list_rows(filters={"name": "x"})
    assert all(g.name == "x" for g in only_x)
    assert len(only_x) == 5

    # offset page
    page = await repo.page(limit=3, offset=0, order_by=["rank"])
    assert page.total == 10
    assert page.has_more is True
    assert [g.rank for g in page.items] == [0, 1, 2]

    # keyset page over the id
    first = await repo.keyset_page(key="id", limit=4)
    assert [g.id for g in first] == ["g00", "g01", "g02", "g03"]
    nxt = await repo.keyset_page(key="id", limit=4, after=Cursor(last_value="g03"))
    assert [g.id for g in nxt] == ["g04", "g05", "g06", "g07"]


async def test_update_where_bulk(session: AsyncSession) -> None:
    repo = GadgetRepo(session)
    await repo.create(id="a", name="grp", rank=1)
    await repo.create(id="b", name="grp", rank=2)
    await repo.create(id="c", name="other", rank=3)
    affected = await repo.update_where({"rank": 100}, name="grp")
    assert affected == 2
    assert (await repo.get_or_raise("a")).rank == 100
    assert (await repo.get_or_raise("c")).rank == 3


# --- soft delete ------------------------------------------------------------


async def test_soft_delete_hides_then_restores(session: AsyncSession) -> None:
    repo = GadgetRepo(session)
    assert repo.supports_soft_delete is True
    await repo.create(id="s1", name="soft")

    assert await repo.delete("s1") is True  # routes to soft_delete
    # Hidden from normal reads...
    assert await repo.get("s1") is None
    assert await repo.count() == 0
    # ...but visible when explicitly included.
    raised = await repo.get("s1", include_deleted=True)
    assert raised is not None and raised.is_deleted is True
    assert await repo.count(include_deleted=True) == 1

    # Restore brings it back.
    assert await repo.restore("s1") is True
    assert await repo.get("s1") is not None
    # Second restore is a no-op.
    assert await repo.restore("s1") is False


async def test_hard_delete_when_unsupported(session: AsyncSession) -> None:
    repo = CounterRepo(session)
    assert repo.supports_soft_delete is False
    await repo.create(id="c1", value=5)
    assert await repo.delete("c1") is True  # routes to hard_delete
    assert await repo.get("c1") is None
    with pytest.raises(TypeError, match="does not support soft delete"):
        await repo.soft_delete("c1")


# --- optimistic concurrency -------------------------------------------------


async def test_optimistic_version_increments(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as db:
        repo = CounterRepo(db)
        c = await repo.create(id="v1", value=0)
        assert c.version_id == 1
        await repo.update("v1", value=1)
        await db.commit()
        assert c.version_id == 2


async def test_optimistic_conflict_raises_stale(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # Seed.
    async with maker() as db:
        await CounterRepo(db).create(id="race", value=0)
        await db.commit()

    # Two sessions load the same version, both try to write → second flush stale.
    s1 = maker()
    s2 = maker()
    try:
        c1 = await CounterRepo(s1).get_or_raise("race")
        c2 = await CounterRepo(s2).get_or_raise("race")
        c1.value = 10
        await s1.commit()  # version 1 -> 2
        c2.value = 20
        with pytest.raises(StaleDataError):
            await s2.commit()  # expects version 1, but it is now 2
    finally:
        await s1.close()
        await s2.rollback()
        await s2.close()


# --- unit of work -----------------------------------------------------------


async def test_uow_commits_and_registry_shares_session(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(maker) as uow:
        gadgets = uow.repo(GadgetRepo)
        # Repo registry returns the same instance on repeat.
        assert uow.repo(GadgetRepo) is gadgets
        assert gadgets.session is uow.session
        await gadgets.create(id="u1", name="committed")

    # Committed by the context exit; visible to a fresh session.
    async with maker() as db:
        assert await GadgetRepo(db).get("u1") is not None


async def test_uow_rolls_back_on_error(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with unit_of_work(maker) as uow:
            await uow.repo(GadgetRepo).create(id="u2", name="doomed")
            raise RuntimeError("boom")
    async with maker() as db:
        assert await GadgetRepo(db).get("u2") is None


async def test_uow_savepoint_isolates_failure(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with unit_of_work(maker) as uow:
        repo = uow.repo(GadgetRepo)
        await repo.create(id="keep", name="outer")
        # Inner savepoint that fails rolls back only its own writes.
        with pytest.raises(RuntimeError):
            async with uow.savepoint():
                await repo.create(id="drop", name="inner")
                raise RuntimeError("inner failure")
        # Outer write survives.
    async with maker() as db:
        assert await GadgetRepo(db).get("keep") is not None
        assert await GadgetRepo(db).get("drop") is None


async def test_run_in_uow_retries_serialization_failure(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    attempts = {"n": 0}

    async def op(uow: UnitOfWork) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            # Simulate a transient serialization failure inside the unit.
            from tests.test_db_retry import _FakePgError

            raise _FakePgError("40001")
        await uow.repo(GadgetRepo).create(id="retry_ok", name="eventually")
        return "done"

    result = await run_in_uow(
        maker, op, policy=RetryPolicy(max_attempts=5, base_backoff_s=0, jitter=False)
    )
    assert result == "done"
    assert attempts["n"] == 3
    async with maker() as db:
        assert await GadgetRepo(db).get("retry_ok") is not None


async def test_run_in_uow_no_retry_reraises(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async def op(_: UnitOfWork) -> None:
        raise ValueError("hard fail")

    with pytest.raises(ValueError, match="hard fail"):
        await run_in_uow(maker, op, retry=False)


def test_retry_class_retryable_flag() -> None:
    assert RetryClass.SERIALIZATION.retryable is True
    assert RetryClass.NON_RETRYABLE.retryable is False
