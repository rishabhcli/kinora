"""A consistency checker: does a live read model match a fresh rebuild?

A projection's read model is derived state, advanced incrementally over a long
catch-up. Bugs, partial rebuilds, or a stale fold version can leave it subtly out
of sync with what a *clean replay of the whole log* would produce. This module
answers the operator's question — "is projection X trustworthy right now?" — by:

1. replaying the entire event log from position 0 through a *fresh copy* of the
   projection into a throwaway :class:`~app.datalayer.readmodel.InMemoryReadModelStore`
   (the **expected** view), and
2. diffing that against the **actual** rows the live projection currently serves.

The diff is row-by-row over ``{key: value}`` deep copies, so it pinpoints
missing, extra, and mismatched rows. The check is read-only against the live
store: it never mutates the running read model.

The replay reuses the same store-agnostic primitives as the runner
(``read_all`` paging + :func:`app.datalayer.envelope.decode` +
:meth:`Projection.apply`), so "a fresh rebuild" here is exactly what
:meth:`~app.datalayer.projector.ProjectionRunner.rebuild` would materialise — the
checker is the rebuild's oracle without touching the live state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.datalayer.envelope import decode
from app.datalayer.projector import Projection, replay_into
from app.datalayer.readmodel import InMemoryReadModelStore, ReadModelStore
from app.eventsourcing.store.contracts import EventStore

logger = get_logger("app.datalayer.consistency")

_PAGE = 256


@dataclass(frozen=True, slots=True)
class RowDiff:
    """One disagreement between the live view and the fresh rebuild."""

    key: str
    #: ``"missing"`` (rebuild has it, live doesn't), ``"extra"`` (live has it,
    #: rebuild doesn't), or ``"mismatch"`` (both have it, values differ).
    kind: str
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """The outcome of a :func:`check_consistency` run."""

    projection: str
    namespace: str
    events_replayed: int
    expected_rows: int
    actual_rows: int
    diffs: list[RowDiff] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """True when the live view matches the fresh rebuild exactly."""
        return not self.diffs

    def summary(self) -> str:
        if self.consistent:
            return (
                f"{self.projection}: consistent "
                f"({self.actual_rows} rows, {self.events_replayed} events replayed)"
            )
        kinds: dict[str, int] = {}
        for d in self.diffs:
            kinds[d.kind] = kinds.get(d.kind, 0) + 1
        parts = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        return f"{self.projection}: INCONSISTENT ({len(self.diffs)} diffs: {parts})"


async def _replay_all(
    projection: Projection,
    event_store: EventStore,
    target: ReadModelStore,
    *,
    namespace: str,
) -> int:
    """Replay the whole log through ``projection`` into ``target``; return event count."""
    position = 0
    replayed = 0
    interested = projection.interested_in()
    while True:
        batch = await event_store.read_all(from_position=position, limit=_PAGE)
        if not batch:
            break
        decoded = []
        for recorded in batch:
            position = recorded.global_position
            event = decode(recorded)
            replayed += 1
            if interested is None or event.type in interested:
                decoded.append(event)
        if decoded:
            await replay_into(projection, decoded, target, namespace=namespace)
        if len(batch) < _PAGE:
            break
    return replayed


async def check_consistency(
    projection: Projection,
    *,
    event_store: EventStore,
    live_read_models: ReadModelStore,
    namespace: str | None = None,
) -> ConsistencyReport:
    """Verify the live read model for ``projection`` matches a fresh rebuild.

    Replays the full log through a fresh copy of ``projection`` into a scratch
    in-memory store and diffs it against the rows the live store currently serves
    in ``namespace``. Read-only against ``live_read_models``.

    The same ``Projection`` *instance* is reused for the replay because handlers
    are stateless (they thread all state through the store); a stateful projection
    should be passed a fresh instance by the caller.
    """
    ns = namespace or projection.namespace
    scratch = InMemoryReadModelStore()
    replayed = await _replay_all(projection, event_store, scratch, namespace=ns)

    expected = scratch.snapshot(ns)
    actual = await _snapshot_live(live_read_models, ns)

    diffs: list[RowDiff] = []
    for key in sorted(set(expected) | set(actual)):
        exp = expected.get(key)
        act = actual.get(key)
        if exp is not None and act is None:
            diffs.append(RowDiff(key=key, kind="missing", expected=exp))
        elif exp is None and act is not None:
            diffs.append(RowDiff(key=key, kind="extra", actual=act))
        elif exp != act:
            diffs.append(RowDiff(key=key, kind="mismatch", expected=exp, actual=act))

    report = ConsistencyReport(
        projection=projection.name,
        namespace=ns,
        events_replayed=replayed,
        expected_rows=len(expected),
        actual_rows=len(actual),
        diffs=diffs,
    )
    if not report.consistent:
        logger.warning(
            "projection_inconsistent",
            projection=projection.name,
            namespace=ns,
            diffs=len(diffs),
        )
    return report


async def _snapshot_live(store: ReadModelStore, namespace: str) -> dict[str, dict[str, Any]]:
    """A ``{key: value}`` snapshot of a namespace through the read-only protocol."""
    out: dict[str, dict[str, Any]] = {}
    for row in await store.list(namespace):
        value = row.value
        out[row.key] = dict(value) if isinstance(value, Mapping) else value
    return out


__all__ = [
    "ConsistencyReport",
    "RowDiff",
    "check_consistency",
]
