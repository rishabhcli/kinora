"""The CLI execution context — owns the wired :class:`Container`.

The whole point of the admin CLI is that it talks to the **same** subsystems the
API does, so it builds the real composition :class:`Container` (lazy: no sockets
open until an action actually queries Postgres / Redis / object storage). A
:class:`CliContext` bundles that container with the chosen output format and a
helper to open a unit-of-work session, and guarantees the container is shut down
(connections closed) when the command finishes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.output import Format
from app.composition import Container, build_container
from app.core.config import Settings


@dataclass(slots=True)
class CliContext:
    """Everything a command needs: the container, the format, the session helper."""

    container: Container
    fmt: Format

    @property
    def settings(self) -> Settings:
        """The active application settings."""
        return self.container.settings

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Open a committing unit-of-work session (the API's transaction boundary)."""
        async with self.container.session_factory() as db:
            yield db

    async def with_session(self, fn: Callable[[AsyncSession], Awaitable[object]]) -> object:
        """Run ``fn`` inside one session and return its result."""
        async with self.session() as db:
            return await fn(db)


@asynccontextmanager
async def build_context(
    fmt: Format, *, settings: Settings | None = None
) -> AsyncIterator[CliContext]:
    """Build a :class:`CliContext` and ensure its container is shut down after use."""
    container = build_container(settings)
    ctx = CliContext(container=container, fmt=fmt)
    try:
        yield ctx
    finally:
        await container.shutdown()


__all__ = ["CliContext", "build_context"]
