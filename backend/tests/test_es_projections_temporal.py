"""Tests for as-of / temporal queries over the event log (§8.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.eventsourcing.projections.contracts import StoredEvent
from app.eventsourcing.projections.examples.canon_audit_view import (
    CanonAuditViewProjection,
)
from app.eventsourcing.projections.memory_eventstore import InMemoryEventStore
from app.eventsourcing.projections.projection import Projection, handles
from app.eventsourcing.projections.readmodel import ReadModelRow, ReadModelStore
from app.eventsourcing.projections.temporal import AsOfProjector, AsOfResult, diff_rows

pytestmark = pytest.mark.asyncio


def _row(result: AsOfResult, key: str) -> ReadModelRow:
    row = result.row(key)
    assert row is not None
    return row


class ValueProjection(Projection):
    name = "value"

    @handles("set")
    async def _on_set(
        self, store: ReadModelStore, ns: str, ev: StoredEvent
    ) -> None:
        await store.put(ns, ev.stream_id, {"v": ev.payload["v"]})

    @handles("delete")
    async def _on_del(
        self, store: ReadModelStore, ns: str, ev: StoredEvent
    ) -> None:
        await store.delete(ns, ev.stream_id)


async def test_at_position_reconstructs_past_view() -> None:
    es = InMemoryEventStore()
    await es.append("k", "set", {"v": "a"})  # pos 1
    await es.append("k", "set", {"v": "b"})  # pos 2
    await es.append("k", "set", {"v": "c"})  # pos 3
    asof = AsOfProjector(event_store=es)
    proj = ValueProjection()
    at1 = await asof.at_position(proj, position=1)
    at2 = await asof.at_position(proj, position=2)
    head = await asof.at_head(proj)
    assert _row(at1, "k").value == {"v": "a"}
    assert _row(at2, "k").value == {"v": "b"}
    assert _row(head, "k").value == {"v": "c"}
    assert head.events_folded == 3


async def test_at_time_uses_transaction_time() -> None:
    es = InMemoryEventStore()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    await es.append("k", "set", {"v": "early"}, recorded_at=t0)
    await es.append("k", "set", {"v": "late"}, recorded_at=t0 + timedelta(hours=2))
    asof = AsOfProjector(event_store=es)
    result = await asof.at_time(ValueProjection(), as_of=t0 + timedelta(hours=1))
    assert _row(result, "k").value == {"v": "early"}  # late event excluded


async def test_as_of_does_not_mutate_live_state() -> None:
    es = InMemoryEventStore()
    await es.append("k", "set", {"v": "x"})
    asof = AsOfProjector(event_store=es)
    proj = ValueProjection()
    await asof.at_head(proj)
    # The projector folds into a throwaway store; calling it twice is stable and
    # leaves no shared state (a second read recomputes from the log).
    second = await asof.at_head(proj)
    assert _row(second, "k").value == {"v": "x"}


async def test_diff_rows_detects_add_remove_modify() -> None:
    es = InMemoryEventStore()
    await es.append("a", "set", {"v": 1})  # pos 1
    await es.append("b", "set", {"v": 2})  # pos 2
    await es.append("a", "set", {"v": 9})  # pos 3 (modify a)
    await es.append("b", "delete", {})  # pos 4 (remove b)
    await es.append("c", "set", {"v": 3})  # pos 5 (add c)
    asof = AsOfProjector(event_store=es)
    proj = ValueProjection()
    before = await asof.at_position(proj, position=2)
    after = await asof.at_head(proj)
    diff = diff_rows(before, after)
    changes = {d.key: d.change for d in diff.diffs}
    assert changes == {"a": "modified", "b": "removed", "c": "added"}
    assert not diff.is_empty


async def test_canon_audit_as_of_shows_pre_retire_state() -> None:
    """§8.5 forgetting: an as-of read before a retire sees the fact un-retired."""
    es = InMemoryEventStore()
    await es.append(
        "canon:alice", "canon.fact_asserted", {"value": "has sword", "valid_from_beat": 20}
    )  # pos 1
    await es.append(
        "canon:alice", "canon.fact_retired", {"valid_to_beat": 30}
    )  # pos 2
    asof = AsOfProjector(event_store=es)
    proj = CanonAuditViewProjection()
    before = await asof.at_position(proj, position=1, stream_id="canon:alice")
    after = await asof.at_head(proj, stream_id="canon:alice")
    assert _row(before, "canon:alice").value["retired"] is False
    assert _row(after, "canon:alice").value["retired"] is True
