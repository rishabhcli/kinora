"""Postgres-backed store + service integration tests (isolated flags DB).

These exercise the durable persistence layer (:class:`~app.flags.store.FlagStore`
/ :class:`~app.flags.store.ExperimentStore` / :class:`~app.flags.service.FlagService`)
against a real Postgres. They skip cleanly when ``KINORA_FLAGS_TEST_DATABASE_URL``
is not set, and use a DEDICATED database (``kinora_flags_test`` on :5433) so they
never touch the live ``kinora`` DB. Each test starts from a clean slate by
TRUNCATE-ing only the four flag tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  register tables on Base.metadata
from app.db.base import Base
from app.flags.context import EvalContext
from app.flags.experiment import Experiment, ExperimentStatus, Variant
from app.flags.models import Flag, Reason
from app.flags.service import FlagService
from app.flags.store import ExperimentStore, FlagStore

_FLAGS_DB_URL = os.environ.get("KINORA_FLAGS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        _FLAGS_DB_URL is None,
        reason="KINORA_FLAGS_TEST_DATABASE_URL not set; skipping flags DB tests",
    ),
]

_FLAG_TABLES = ("flag_audit", "flag_exposures", "flag_experiments", "feature_flags")


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert _FLAGS_DB_URL is not None
    engine = create_async_engine(_FLAGS_DB_URL, poolclass=NullPool)
    # Ensure schema exists + start clean (only our tables).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(f"TRUNCATE {', '.join(_FLAG_TABLES)} RESTART IDENTITY CASCADE")
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@asynccontextmanager
async def _uow(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A committing unit of work (the service writes durably)."""
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def test_flag_save_load_version_and_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _uow(session_factory) as s:
        store = FlagStore(s)
        saved = await store.save(Flag.boolean("live-video", enabled=True), actor="alice")
        assert saved.version == 1

    async with _uow(session_factory) as s:
        store = FlagStore(s)
        loaded = await store.get("live-video")
        assert loaded is not None and loaded.enabled is True
        # second save bumps version + writes another audit row
        await store.save(Flag.boolean("live-video", enabled=False), actor="bob")

    async with session_factory() as s:
        store = FlagStore(s)
        again = await store.get("live-video")
        assert again is not None and again.version == 2 and again.enabled is False
        log = await store.audit_log(subject_key="live-video")
        assert len(log) == 2
        actions = {r.action for r in log}
        assert "create" in actions
        # an enabled-only flip is classified as a toggle
        assert "toggle" in actions


async def test_snapshot_load(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with _uow(session_factory) as s:
        store = FlagStore(s)
        await store.save(Flag.boolean("a"))
        await store.save(Flag.boolean("b"))
    async with session_factory() as s:
        snap = await FlagStore(s).load_snapshot(version=7)
        assert set(snap.keys()) == {"a", "b"}
        assert snap.version == 7


async def test_archive_and_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _uow(session_factory) as s:
        store = FlagStore(s)
        await store.save(Flag.boolean("temp"))
        await store.archive("temp", actor="alice")
    async with _uow(session_factory) as s:
        store = FlagStore(s)
        flag = await store.get("temp")
        assert flag is not None and flag.archived is True
        # archived flags excluded from the default listing
        assert "temp" not in {f.key for f in await store.list_all()}
        assert "temp" in {f.key for f in await store.list_all(include_archived=True)}
        deleted = await store.delete("temp")
        assert deleted is True
    async with session_factory() as s:
        assert await FlagStore(s).get("temp") is None


async def test_exposure_logging_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _uow(session_factory) as s:
        store = ExperimentStore(s)
        inserted_1 = await store.log_exposure(
            experiment_key="ab",
            experiment_version=1,
            variant_key="treatment",
            unit_key="u1",
            dedup_key="ab:v1:u1",
        )
        inserted_2 = await store.log_exposure(
            experiment_key="ab",
            experiment_version=1,
            variant_key="treatment",
            unit_key="u1",
            dedup_key="ab:v1:u1",  # same dedup key
        )
        assert inserted_1 is True
        assert inserted_2 is False  # conflict -> no new row
    async with session_factory() as s:
        counts = await ExperimentStore(s).exposure_counts("ab")
        assert counts == {"treatment": 1}


async def test_service_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = FlagService(
        lambda: _uow(session_factory), default_salt="kinora", cache_ttl_s=0.0
    )
    await svc.upsert_flag(Flag.boolean("live-video", enabled=True, rollout_percent=100.0))
    ev = await svc.evaluate("live-video", EvalContext.of("reader-1"))
    assert ev.value is True
    assert ev.reason is Reason.FALLTHROUGH

    # an experiment, assigned + exposure-logged durably
    exp = Experiment(
        key="crew-vs-baseline",
        variants=(Variant("baseline", 5000, is_control=True), Variant("crew", 5000)),
        salt="ccs",
        status=ExperimentStatus.RUNNING,
    )
    await svc.upsert_experiment(exp)
    for i in range(60):
        a = await svc.assign("crew-vs-baseline", EvalContext.of(f"reader-{i}"))
        assert a is not None and a.in_experiment
    # idempotent: re-assign the same readers
    for i in range(20):
        await svc.assign("crew-vs-baseline", EvalContext.of(f"reader-{i}"))
    counts = await svc.exposure_counts("crew-vs-baseline")
    assert sum(counts.values()) == 60
