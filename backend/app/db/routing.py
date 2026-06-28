"""Read/write split — route a session to the writer or a read-replica.

A long-lived deployment with a Postgres read-replica wants reads (the scroll
resolution, episodic search, the library shelf) served by the replica and writes
(canon upserts, the budget ledger, render-job transitions) served by the
primary. This module turns an :class:`~app.db.engine.EngineRegistry` into two
session factories — one bound to each engine — and a small :class:`RoutingSession`
helper that picks the right one by *intent*.

The split is **opt-in and safe**: when no replica is configured the registry's
``reader()`` returns the primary, so :meth:`RoutingSessionFactory.read` behaves
identically to :meth:`write` on a single-node deployment. Callers can therefore
always express their intent ("this is a read") and get the optimal routing
wherever they run, with no code change between dev (one node) and prod (a
replica). The unit-of-work commit/rollback contract is preserved exactly as
:func:`app.db.session.get_session` defines it.

**Correctness rule the caller owns:** a replica is asynchronously replicated, so
reading your own just-committed write from the replica may lag. Anything that
must read-after-write (e.g. resolve a shot you just inserted) routes to the
writer. The default for ambiguous cases is the writer — never silently serve a
possibly-stale read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import EngineRegistry


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an async session factory bound to ``engine`` (Kinora's UoW defaults).

    ``expire_on_commit=False`` keeps committed instances usable after the
    transaction (the gateway returns ORM rows straight to response models);
    ``autoflush=False`` matches the existing factory so repositories control
    flush timing.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@dataclass(slots=True)
class RoutingSessionFactory:
    """Two committing unit-of-work factories — one for writes, one for reads.

    Built from an :class:`EngineRegistry`. ``write()`` always binds the primary;
    ``read()`` binds the replica when one is configured and the primary otherwise.
    Both yield an :class:`AsyncSession`, commit on clean exit, and roll back on
    error — the same boundary :func:`app.db.session.get_session` establishes.

    Read sessions still *can* write (Postgres replicas reject writes at the
    server, which surfaces as an error), but the intent is advisory: routing a
    write through ``read()`` on a single node will simply commit to the primary.
    """

    registry: EngineRegistry
    _writer_maker: async_sessionmaker[AsyncSession] | None = None
    _reader_maker: async_sessionmaker[AsyncSession] | None = None

    def writer_maker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory bound to the primary engine."""
        if self._writer_maker is None:
            self._writer_maker = make_sessionmaker(self.registry.writer())
        return self._writer_maker

    def reader_maker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory bound to the reader (replica or primary fallback)."""
        if self._reader_maker is None:
            reader_engine = self.registry.reader()
            # When there is no replica the reader *is* the writer engine; reuse
            # the writer maker so we don't open a second identical factory.
            if not self.registry.has_replica:
                self._reader_maker = self.writer_maker()
            else:
                self._reader_maker = make_sessionmaker(reader_engine)
        return self._reader_maker

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """Open a writer-bound unit of work (commit on success, rollback on error)."""
        async with self._unit_of_work(self.writer_maker()) as session:
            yield session

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        """Open a reader-bound unit of work (replica when configured).

        Reads commit too: a read transaction may still touch session-local state
        (e.g. ``SET LOCAL``), and committing an empty read transaction is cheap
        and keeps the boundary uniform with :meth:`write`.
        """
        async with self._unit_of_work(self.reader_maker()) as session:
            yield session

    def session(self, *, readonly: bool = False) -> AbstractAsyncContextManager[AsyncSession]:
        """Open a session by intent: ``readonly=True`` routes to the reader.

        Returns the same ``async with`` context manager as :meth:`read`/:meth:`write`
        — handy when the read/write decision is computed rather than literal.
        """
        maker = self.reader_maker() if readonly else self.writer_maker()
        return self._unit_of_work(maker)

    @staticmethod
    @asynccontextmanager
    async def _unit_of_work(
        maker: async_sessionmaker[AsyncSession],
    ) -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @classmethod
    def from_registry(cls, registry: EngineRegistry) -> RoutingSessionFactory:
        """Build a routing factory over an existing engine registry."""
        return cls(registry=registry)


__all__ = [
    "RoutingSessionFactory",
    "make_sessionmaker",
]
