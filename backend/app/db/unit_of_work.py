"""The Unit-of-Work boundary: transaction ownership, savepoints, and retries.

:func:`app.db.session.get_session` already establishes the basic boundary —
commit on clean exit, roll back on error, repositories only ``flush``. This
module formalises that into a reusable :class:`UnitOfWork` that adds:

* **a repository registry** — ``uow.repo(BookRepo)`` lazily constructs and caches
  a repository bound to the unit's session, so a multi-repository operation
  shares one transaction without threading the session through every call;
* **savepoints** — ``async with uow.savepoint():`` opens a nested transaction
  (``SAVEPOINT``) that can roll back independently, for "try this, fall back on
  conflict" sub-steps inside one outer transaction;
* **retry-on-serialization-failure** — :func:`run_in_uow` runs a whole operation
  inside a fresh unit of work and re-runs it on a transient MVCC failure
  (§12-grade robustness), reusing the :mod:`app.db.retry` classifier.

It is built over a session *factory* (``async_sessionmaker`` or the routing
factory's maker), so a retry can open a brand-new session — never re-using one
that was already rolled back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.base import BaseRepository
from app.db.retry import DEFAULT_RETRY_POLICY, RetryPolicy, with_db_retry

RepoT = TypeVar("RepoT", bound=BaseRepository)
T = TypeVar("T")


class UnitOfWork:
    """One transactional scope over an :class:`AsyncSession`.

    Enter the context to begin; on clean exit it commits, on an exception it
    rolls back and re-raises. Repositories are obtained via :meth:`repo` so they
    all share this unit's session (and therefore its transaction). Never commit
    from inside a repository — the unit owns that.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repos: dict[type[BaseRepository], BaseRepository] = {}
        self._closed = False

    @property
    def session(self) -> AsyncSession:
        """The session every repository in this unit shares."""
        return self._session

    def repo(self, repo_cls: type[RepoT]) -> RepoT:
        """Return a (cached) repository of ``repo_cls`` bound to this unit's session."""
        existing = self._repos.get(repo_cls)
        if existing is None:
            existing = repo_cls(self._session)
            self._repos[repo_cls] = existing
        return cast(RepoT, existing)

    async def flush(self) -> None:
        """Flush pending changes (surface constraint errors / populate defaults)."""
        await self._session.flush()

    async def commit(self) -> None:
        """Commit the transaction (normally done automatically on context exit)."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the transaction."""
        await self._session.rollback()

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[AsyncSession]:
        """Open a nested transaction (``SAVEPOINT``) that rolls back independently.

        Use for a sub-step you want to attempt and discard without losing the
        outer transaction: an exception inside the block releases the savepoint
        (rolling back just its writes) and propagates, leaving the outer unit
        intact for the caller to handle.
        """
        async with self._session.begin_nested():
            yield self._session

    # -- async context manager ---------------------------------------------- #

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if exc is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


@asynccontextmanager
async def unit_of_work(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[UnitOfWork]:
    """Open a :class:`UnitOfWork` over a fresh session from ``maker``."""
    session = maker()
    async with UnitOfWork(session) as uow:
        yield uow


async def run_in_uow(
    maker: async_sessionmaker[AsyncSession],
    operation: Callable[[UnitOfWork], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    retry: bool = True,
) -> T:
    """Run ``operation`` inside a unit of work, optionally retrying transient failures.

    ``operation`` receives the :class:`UnitOfWork` and returns a result. The whole
    block (open session → operation → commit) is retried as a unit on a
    serialization failure / deadlock / disconnect, so the operation must be
    idempotent at the transaction boundary (it re-runs from a clean session).
    Set ``retry=False`` to run exactly once.
    """

    async def _once() -> T:
        async with unit_of_work(maker) as uow:
            return await operation(uow)

    if not retry:
        return await _once()
    return await with_db_retry(_once, policy=policy)


__all__ = [
    "UnitOfWork",
    "run_in_uow",
    "unit_of_work",
]
