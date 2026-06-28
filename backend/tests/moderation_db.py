"""Isolated-DB fixtures for the moderation subsystem's persistence tests.

These tests need a real Postgres (the repos, the hash-chained audit log, the
rolling escalation tally). To stay strictly isolated from the live ``kinora`` DB
and from sibling agents' integration runs, they use a **dedicated** database:

    postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_mod_test

resolvable via ``KINORA_MOD_TEST_DATABASE_URL`` (or the default above). When the
DB is unreachable the fixtures **skip cleanly**, so the unit suite still runs
anywhere with no infra.

The session fixture creates the schema once, then TRUNCATEs the moderation tables
(+ users/books it FKs to) before each test so every test starts clean — mirroring
the project's ``conftest._isolate_state`` discipline but scoped to this DB.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register every table on Base.metadata)
from app.db.base import Base

_DEFAULT_URL = "postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_mod_test"
MOD_DB_URL = os.environ.get("KINORA_MOD_TEST_DATABASE_URL", _DEFAULT_URL)

# Tables this subsystem writes (plus the parents it FKs to), TRUNCATEd per test.
_TABLES = (
    "moderation_violation_counters",
    "moderation_tenant_policies",
    "moderation_audit",
    "moderation_review_items",
    "moderation_events",
    "books",
    "users",
)


async def _reachable(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connection failure → skip cleanly
        return False
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def mod_session() -> AsyncIterator[AsyncSession]:
    """A committing session against the isolated moderation test DB (or skip)."""
    if not await _reachable(MOD_DB_URL):
        pytest.skip(f"moderation DB not reachable at {MOD_DB_URL}")
    engine = create_async_engine(MOD_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # The shared metadata includes pgvector columns (entities/shots); the
            # extension must exist before create_all even though moderation does
            # not use it.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            present = ", ".join(
                f'"{t}"'
                for t in _TABLES
                if t in Base.metadata.tables
            )
            if present:
                await conn.execute(text(f"TRUNCATE {present} RESTART IDENTITY CASCADE"))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
            await session.commit()
    finally:
        await engine.dispose()
