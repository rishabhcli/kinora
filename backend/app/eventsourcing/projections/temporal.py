"""As-of / temporal queries over the event log (kinora.md §8.5).

§8.5 frames forgetting as *scoping a fact to the interval where it was true* —
"the stale truth is preserved for time-travel reads (the reader can scroll
*back*) but is invisible to forward generation." Event sourcing makes that
literal: the log is the full history, so any read model can be reconstructed *as
it stood* at a past point simply by folding the prefix of the log up to that
point. This module is the read side's time machine.

Two axes of "as of", matching the bitemporal model (§8):

* **By global position** (:meth:`AsOfProjector.at_position`) — "the view after
  the first N events". Deterministic and exact; the natural cursor for "scroll
  back to where the reader was".
* **By transaction time** (:meth:`AsOfProjector.at_time`) — "the view as the
  system believed it at instant T", using each event's ``recorded_at``. This is
  the wall-clock audit axis.

An as-of read **never mutates** the live read model: it folds into a throwaway
in-memory store and returns the rows. So it is safe to call concurrently with the
the live runtime, costs nothing persistent, and works for *any* projection (it
reuses the projection's own ``apply``). The cost is O(events up to the cutoff);
for hot paths a projection can instead keep position-stamped snapshots, but the
replay path is the always-correct fallback and the one the audit view uses.

:func:`diff_rows` compares two as-of materialisations to answer "what changed in
this view between position A and position B" — the backbone of the canon audit
view's "what did this edit actually change" panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.eventsourcing.projections.contracts import (
    NO_POSITION,
    EventStore,
    GlobalPosition,
    StoredEvent,
)
from app.eventsourcing.projections.projection import Projection
from app.eventsourcing.projections.readmodel import (
    InMemoryReadModelStore,
    ReadModelRow,
)


@dataclass(slots=True)
class AsOfResult:
    """A materialised read model at a point in (log) time."""

    namespace: str
    position: GlobalPosition
    as_of_time: datetime | None
    rows: list[ReadModelRow]
    events_folded: int

    def row(self, key: str) -> ReadModelRow | None:
        return next((r for r in self.rows if r.key == key), None)

    def values(self) -> dict[str, dict[str, Any]]:
        """``{key: value}`` for every materialised row (order preserved)."""
        return {r.key: r.value for r in self.rows}


class AsOfProjector:
    """Reconstructs any projection's read model as of a past position / time."""

    _ASOF_NAMESPACE = "__asof__"

    def __init__(self, *, event_store: EventStore) -> None:
        self._events = event_store

    async def at_position(
        self,
        projection: Projection,
        *,
        position: GlobalPosition,
        stream_id: str | None = None,
    ) -> AsOfResult:
        """Fold the log prefix ``global_position <= position`` into a throwaway view.

        ``stream_id`` optionally restricts the fold to one aggregate's stream (a
        per-entity as-of read, e.g. one character's canon at a past beat-write).
        """
        events = await self._collect(stream_id=stream_id, max_position=position, as_of_time=None)
        return await self._fold(projection, events, position=position, as_of_time=None)

    async def at_time(
        self,
        projection: Projection,
        *,
        as_of: datetime,
        stream_id: str | None = None,
    ) -> AsOfResult:
        """Fold every event ``recorded_at <= as_of`` into a throwaway view."""
        events = await self._collect(stream_id=stream_id, max_position=None, as_of_time=as_of)
        position = events[-1].global_position if events else NO_POSITION
        return await self._fold(projection, events, position=position, as_of_time=as_of)

    async def at_head(
        self, projection: Projection, *, stream_id: str | None = None
    ) -> AsOfResult:
        """The current view (fold everything) — useful as the RHS of a diff."""
        head = await self._events.head_position()
        return await self.at_position(projection, position=head, stream_id=stream_id)

    # -- internals ----------------------------------------------------------- #

    async def _collect(
        self,
        *,
        stream_id: str | None,
        max_position: GlobalPosition | None,
        as_of_time: datetime | None,
    ) -> list[StoredEvent]:
        if stream_id is not None:
            events = await self._events.read_stream(stream_id, as_of=as_of_time)
            if max_position is not None:
                events = [e for e in events if e.global_position <= max_position]
            return events
        # Whole log: read all, then bound by position and/or transaction time.
        events = await self._events.read_all(after_position=NO_POSITION)
        if max_position is not None:
            events = [e for e in events if e.global_position <= max_position]
        if as_of_time is not None:
            events = [
                e for e in events if e.recorded_at is not None and e.recorded_at <= as_of_time
            ]
        return events

    async def _fold(
        self,
        projection: Projection,
        events: list[StoredEvent],
        *,
        position: GlobalPosition,
        as_of_time: datetime | None,
    ) -> AsOfResult:
        scratch = InMemoryReadModelStore()
        for event in events:
            await projection.apply(scratch, self._ASOF_NAMESPACE, event)
        rows = await scratch.list(self._ASOF_NAMESPACE)
        return AsOfResult(
            namespace=projection.namespace,
            position=position,
            as_of_time=as_of_time,
            rows=rows,
            events_folded=len(events),
        )


@dataclass(slots=True)
class RowDiff:
    """How one key changed between two as-of materialisations."""

    key: str
    change: str  # "added" | "removed" | "modified"
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


@dataclass(slots=True)
class ViewDiff:
    """The full set of changes between two as-of points for one projection."""

    from_position: GlobalPosition
    to_position: GlobalPosition
    diffs: list[RowDiff] = field(default_factory=list)

    @property
    def changed_keys(self) -> list[str]:
        return [d.key for d in self.diffs]

    @property
    def is_empty(self) -> bool:
        return not self.diffs


def diff_rows(before: AsOfResult, after: AsOfResult) -> ViewDiff:
    """Compute the per-key diff between two materialisations of the same view."""
    before_map = before.values()
    after_map = after.values()
    diffs: list[RowDiff] = []
    for key in sorted(set(before_map) | set(after_map)):
        b = before_map.get(key)
        a = after_map.get(key)
        if b is None and a is not None:
            diffs.append(RowDiff(key=key, change="added", after=a))
        elif b is not None and a is None:
            diffs.append(RowDiff(key=key, change="removed", before=b))
        elif b != a:
            diffs.append(RowDiff(key=key, change="modified", before=b, after=a))
    return ViewDiff(from_position=before.position, to_position=after.position, diffs=diffs)


__all__ = [
    "AsOfProjector",
    "AsOfResult",
    "RowDiff",
    "ViewDiff",
    "diff_rows",
]
