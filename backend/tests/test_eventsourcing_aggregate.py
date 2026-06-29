"""Unit tests for the aggregate-root base + repository: replay, emit, version
bookkeeping, and the optimistic-concurrency save path. Pure + in-memory store."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.events import DomainEvent, register_events
from app.eventsourcing.domain.identifiers import StreamCategory
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.protocol import ConcurrencyError


@dataclass(frozen=True, slots=True)
class _Incremented(DomainEvent):
    by: int = 1


register_events(_Incremented)


@dataclass(slots=True)
class _Counter(AggregateRoot):
    category = StreamCategory.SESSION
    total: int = 0

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self.total = 0

    def increment(self, by: int = 1) -> None:
        self.emit(_Incremented(by=by))

    def apply(self, event: DomainEvent) -> None:
        if isinstance(event, _Incremented):
            self.total += event.by


def test_fresh_aggregate_starts_empty() -> None:
    c = _Counter("c1")
    assert c.version == 0
    assert c.exists is False
    assert c.uncommitted == ()


def test_emit_queues_and_folds() -> None:
    c = _Counter("c1")
    c.increment(2)
    c.increment(3)
    assert c.total == 5  # folded immediately
    assert c.version == 2
    assert c.expected_version == 0  # nothing committed yet
    assert [e.by for e in c.uncommitted if isinstance(e, _Incremented)] == [2, 3]


def test_replay_rebuilds_state() -> None:
    c = _Counter("c1")
    c.replay([_Incremented(by=4), _Incremented(by=6)])
    assert c.total == 10
    assert c.version == 2
    assert c.expected_version == 2  # replayed events are committed
    assert c.uncommitted == ()


def test_mark_committed_clears_uncommitted() -> None:
    c = _Counter("c1")
    c.increment(1)
    c.mark_committed()
    assert c.uncommitted == ()
    assert c.expected_version == 1


def test_stream_id_uses_category() -> None:
    c = _Counter("c1")
    assert c.stream_id.value == "session-c1"


async def test_repository_save_and_load_round_trip() -> None:
    store = InMemoryEventStore()
    repo: Repository[_Counter] = Repository(store, _Counter)

    c = _Counter("c1")
    c.increment(5)
    c.increment(7)
    await repo.save(c)
    assert c.uncommitted == ()
    assert c.expected_version == 2

    reloaded = await repo.load("c1")
    assert reloaded.total == 12
    assert reloaded.version == 2
    assert reloaded.expected_version == 2


async def test_repository_save_no_events_is_noop() -> None:
    store = InMemoryEventStore()
    repo: Repository[_Counter] = Repository(store, _Counter)
    c = _Counter("c1")
    result = await repo.save(c)
    assert result.last_version == 0


async def test_repository_optimistic_conflict_on_concurrent_save() -> None:
    store = InMemoryEventStore()
    repo: Repository[_Counter] = Repository(store, _Counter)

    # Two readers load the same (empty) aggregate independently.
    a = await repo.load("c1")
    b = await repo.load("c1")
    a.increment(1)
    b.increment(2)

    await repo.save(a)  # a wins; stream now at version 1
    with pytest.raises(ConcurrencyError):
        await repo.save(b)  # b expected version 0 -> conflict


async def test_repository_save_with_metadata_count_mismatch() -> None:
    store = InMemoryEventStore()
    repo: Repository[_Counter] = Repository(store, _Counter)
    c = _Counter("c1")
    c.increment(1)
    with pytest.raises(ValueError, match="metadata count"):
        await repo.save_with_metadata(c, [])


def test_unknown_event_in_apply_is_ignored() -> None:
    @dataclass(frozen=True, slots=True)
    class _Unknown(DomainEvent):
        pass

    c = _Counter("c1")
    c.replay([_Unknown()])  # _Counter.apply ignores it
    assert c.total == 0
    assert c.version == 1
