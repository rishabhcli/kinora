"""Read service over the materialised views.

A thin, framework-agnostic façade that the API layer (or any consumer) can call
to read a denormalised view by name, with optional filtering and a checkpoint
fallback. It deliberately does **not** register any HTTP route — wiring a route
would touch shared API files; this is the additive seam a route would call.

* In-process reads come straight from a live :class:`MaterializedViewEngine`
  (O(1) — the view is already materialised).
* When the engine isn't resident (a separate read replica process), the service
  can fall back to a :class:`ViewStateCheckpointStore`, serving the last
  persisted snapshot of the view.

Filtering is a simple equality match over output columns — enough for the common
"shelf for this owner" / "shots for this book" reads; richer querying belongs in
the consumer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.streaming.cdc.db_adapters import ViewStateCheckpointStore
from app.streaming.cdc.views.engine import MaterializedViewEngine


class ViewNotFoundError(KeyError):
    """Raised when a requested view name isn't registered/persisted."""


class ViewReadService:
    """Read denormalised view rows from a live engine (with a checkpoint fallback)."""

    def __init__(
        self,
        engine: MaterializedViewEngine | None = None,
        *,
        checkpoint: ViewStateCheckpointStore | None = None,
    ) -> None:
        self._engine = engine
        self._checkpoint = checkpoint

    @property
    def views(self) -> set[str]:
        return self._engine.graph.views if self._engine is not None else set()

    async def read(
        self,
        view: str,
        *,
        where: Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows of ``view``, optionally equality-filtered and limited."""
        rows = await self._rows(view)
        if where:
            rows = [r for r in rows if _matches(r, where)]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def count(self, view: str, *, where: Mapping[str, Any] | None = None) -> int:
        return len(await self.read(view, where=where))

    async def _rows(self, view: str) -> list[dict[str, Any]]:
        if self._engine is not None and view in self._engine.graph.views:
            return self._engine.rows(view)
        if self._checkpoint is not None:
            persisted = await self._checkpoint.rows(view)
            if persisted:
                return [dict(r) for r in persisted]
            return []
        raise ViewNotFoundError(view)


def _matches(row: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    return all(row.get(k) == v for k, v in where.items())


def matches_any(row: Mapping[str, Any], column: str, values: Iterable[Any]) -> bool:
    """Helper: whether ``row[column]`` is in ``values`` (for IN-style filters)."""
    return row.get(column) in set(values)


__all__ = ["ViewNotFoundError", "ViewReadService", "matches_any"]
