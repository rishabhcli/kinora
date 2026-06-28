"""Billing integration-test fixtures.

These tests need a real Postgres (the repos use SAVEPOINT-based idempotency and
windowed-sum aggregation that an in-memory fake can't faithfully reproduce). They
run against an **isolated** database — ``KINORA_BILLING_TEST_DATABASE_URL`` if
set, else falling back to ``KINORA_TEST_DATABASE_URL`` — and **skip cleanly** when
neither is configured, so the default unit suite still runs anywhere.

Isolation mirrors the root ``conftest._isolate_state``: ensure the schema, then
TRUNCATE every ``billing_*`` table before each test. We never touch the live
``kinora`` database — the task provisions ``kinora_billing_test`` on :5433.
"""

from __future__ import annotations

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("APP_ENV", "local")
os.environ.setdefault("REASONING_PROVIDER", "dashscope")

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.composition import make_session_factory
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base

_BILLING_DB_URL = os.environ.get("KINORA_BILLING_TEST_DATABASE_URL") or os.environ.get(
    "KINORA_TEST_DATABASE_URL"
)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_SKIP_REASON = (
    "billing integration tests need KINORA_BILLING_TEST_DATABASE_URL (or _TEST_DATABASE_URL)"
)
requires_billing_db = pytest.mark.skipif(not _BILLING_DB_URL, reason=_SKIP_REASON)

_BILLING_TABLES = [t for t in Base.metadata.sorted_tables if t.name.startswith("billing_")]


async def _ensure_and_truncate() -> None:
    assert _BILLING_DB_URL is not None
    engine = create_async_engine(_BILLING_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            names = ", ".join(f'"{t.name}"' for t in _BILLING_TABLES)
            if names:
                await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _isolate_billing() -> AsyncIterator[None]:
    if _BILLING_DB_URL:
        await _ensure_and_truncate()
    yield


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[SessionFactory]:
    """A committing unit-of-work factory bound to the isolated billing DB."""
    if not _BILLING_DB_URL:
        pytest.skip("billing integration tests require an isolated billing DB")
    engine = create_async_engine(_BILLING_DB_URL, poolclass=NullPool)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield make_session_factory(maker)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def make_user(session_factory: SessionFactory) -> Callable[..., object]:
    """Factory creating a real ``users`` row (so customer FKs are satisfiable)."""
    from app.db.base import new_id
    from app.db.models.user import User

    async def _make(email: str | None = None) -> str:
        uid = new_id()
        async with session_factory() as db:
            db.add(
                User(
                    id=uid,
                    email=email or f"{uid}@example.com",
                    hashed_password="x",
                )
            )
        return uid

    return _make
