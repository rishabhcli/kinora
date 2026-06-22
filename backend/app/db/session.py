"""Async SQLAlchemy engine, session factory, and the request-scoped session.

The engine and session factory are created lazily (memoised) so importing this
module has no side effects and tests can point a *different* engine at a
throwaway database. :func:`get_session` is both an ``async with`` context
manager and a FastAPI dependency: it yields a session, commits on clean exit,
and rolls back on error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (created on first use)."""
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory (created on first use)."""
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession`, committing on success and rolling back on error.

    Usable directly as ``async with get_session() as session:`` and as a FastAPI
    dependency (``Depends(get_session)``). Repositories never commit on their
    own; this unit-of-work boundary owns the transaction.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the cached engine's connection pool (call on app shutdown)."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
