"""The read-side query facade — the single entry point an API layer calls.

Everything else in this package is machinery; :class:`ProjectionReader` is the
*read API*. It resolves a logical projection name to the namespace currently
serving reads (honouring blue/green swaps), optionally enforces read-your-writes
against a :class:`ConsistencyToken`, and returns the materialised rows — so a
FastAPI route never has to know about slots, checkpoints, or lag.

Typical use from a route::

    reader = ProjectionReader(registry)
    row = await reader.get("session_timeline", "session:s1", token=ryw_token)
    board = await reader.list("shot_status_board")

Read-your-writes: pass the :class:`ConsistencyToken` a command handed back. The
reader awaits the projection catching up to the token's position (bounded by
``ryw_timeout_s``); on timeout it returns the (possibly stale) data with a
:class:`ReadResult.stale` flag set rather than blocking forever, so the caller
can decide whether to surface a "still catching up" hint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.eventsourcing.projections.lag import ConsistencyToken
from app.eventsourcing.projections.readmodel import ReadModelRow
from app.eventsourcing.projections.registry import ProjectionRegistry


@dataclass(slots=True)
class ReadResult:
    """A read-model lookup result with consistency metadata."""

    rows: list[ReadModelRow]
    #: True if a read-your-writes wait timed out and the data may be stale.
    stale: bool = False
    #: The projection's checkpoint position at read time (for client cursors).
    position: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def one(self) -> ReadModelRow | None:
        """The single row, if exactly one was returned (else ``None``)."""
        return self.rows[0] if len(self.rows) == 1 else None


class ProjectionReader:
    """Slot-aware, RYW-aware read facade over a :class:`ProjectionRegistry`."""

    def __init__(self, registry: ProjectionRegistry, *, ryw_timeout_s: float = 5.0) -> None:
        self._registry = registry
        self._ryw_timeout_s = ryw_timeout_s

    async def _resolve_namespace(self, projection: str) -> str:
        """The namespace currently serving reads for ``projection`` (blue/green aware)."""
        return await self._registry.rebuilder().active_namespace(projection)

    async def _await_token(self, projection: str, token: ConsistencyToken | None) -> bool:
        """Read-your-writes gate: returns True if stale (timed out), else False."""
        if token is None:
            return False
        tracker = self._registry.lag_tracker()
        caught = await tracker.wait_for(
            token, projection=projection, timeout_s=self._ryw_timeout_s
        )
        return not caught

    async def get(
        self,
        projection: str,
        key: str,
        *,
        token: ConsistencyToken | None = None,
    ) -> ReadResult:
        """Fetch a single row, honouring blue/green slot + optional RYW token."""
        stale = await self._await_token(projection, token)
        namespace = await self._resolve_namespace(projection)
        row = await self._registry.read_models.get(namespace, key)
        position = (await self._registry.checkpoint_store.load(projection)).position
        return ReadResult(rows=[row] if row else [], stale=stale, position=position)

    async def list(
        self,
        projection: str,
        *,
        prefix: str | None = None,
        limit: int | None = None,
        token: ConsistencyToken | None = None,
    ) -> ReadResult:
        """Fetch a slice of a projection's rows (ordered by key)."""
        stale = await self._await_token(projection, token)
        namespace = await self._resolve_namespace(projection)
        rows = await self._registry.read_models.list(namespace, prefix=prefix, limit=limit)
        position = (await self._registry.checkpoint_store.load(projection)).position
        return ReadResult(rows=rows, stale=stale, position=position)


__all__ = ["ProjectionReader", "ReadResult"]
