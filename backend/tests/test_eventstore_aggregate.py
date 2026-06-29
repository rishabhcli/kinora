"""Aggregate rehydration tests (zero infra).

Exercises :class:`AggregateRepository` over the in-memory event + snapshot store
with a sample event-sourced aggregate (a simple "bank account" / counter), to
verify: empty-load, fold over events, optimistic-concurrency on append,
snapshot acceleration (replay only the tail after a snapshot), and the
``should_snapshot`` cadence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.eventsourcing.store import (
    NO_EVENTS,
    NO_STREAM,
    Aggregate,
    AggregateRepository,
    EventData,
    InMemoryEventStore,
    OptimisticConcurrencyError,
    RecordedEvent,
    SnapshotStrategy,
)

# --------------------------------------------------------------------------- #
# A sample event-sourced aggregate: a running balance.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Account:
    balance: int = 0
    txns: int = 0


def _apply(state: Account, event: RecordedEvent) -> Account:
    if event.event_type == "deposited":
        return replace(state, balance=state.balance + event.payload["amount"], txns=state.txns + 1)
    if event.event_type == "withdrawn":
        return replace(state, balance=state.balance - event.payload["amount"], txns=state.txns + 1)
    return state


def _account_aggregate() -> Aggregate[Account]:
    return Aggregate(
        initial=Account,
        apply=_apply,
        serialize=lambda a: {"balance": a.balance, "txns": a.txns},
        deserialize=lambda d: Account(balance=d["balance"], txns=d["txns"]),
        snapshot_type="account",
    )


def _deposit(n: int) -> EventData:
    return EventData(event_type="deposited", payload={"amount": n})


def _withdraw(n: int) -> EventData:
    return EventData(event_type="withdrawn", payload={"amount": n})


# --------------------------------------------------------------------------- #
# Load + fold
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_missing_aggregate_is_initial() -> None:
    store = InMemoryEventStore()
    repo = AggregateRepository(_account_aggregate(), store)
    loaded = await repo.load("acct-1")
    assert loaded.state == Account()
    assert loaded.version == NO_EVENTS
    assert not loaded.exists


@pytest.mark.asyncio
async def test_load_folds_events() -> None:
    store = InMemoryEventStore()
    await store.append(
        "acct-1", [_deposit(100), _withdraw(30), _deposit(5)], expected_version=NO_STREAM
    )
    repo = AggregateRepository(_account_aggregate(), store)
    loaded = await repo.load("acct-1")
    assert loaded.state == Account(balance=75, txns=3)
    assert loaded.version == 2
    assert loaded.exists


@pytest.mark.asyncio
async def test_load_pages_long_stream() -> None:
    store = InMemoryEventStore()
    await store.append("acct-1", [_deposit(1) for _ in range(250)], expected_version=NO_STREAM)
    # read_batch smaller than the stream forces multiple read pages.
    repo = AggregateRepository(_account_aggregate(), store, read_batch=50)
    loaded = await repo.load("acct-1")
    assert loaded.state.balance == 250
    assert loaded.state.txns == 250
    assert loaded.version == 249


# --------------------------------------------------------------------------- #
# Append + OCC
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_returns_new_state_and_version() -> None:
    store = InMemoryEventStore()
    repo = AggregateRepository(_account_aggregate(), store)
    loaded = await repo.append("acct-1", [_deposit(100)], expected_version=NO_STREAM)
    assert loaded.state == Account(balance=100, txns=1)
    assert loaded.version == 0

    loaded2 = await repo.append(
        "acct-1", [_withdraw(40)], expected_version=loaded.version
    )
    assert loaded2.state == Account(balance=60, txns=2)
    assert loaded2.version == 1


@pytest.mark.asyncio
async def test_append_with_stale_version_conflicts() -> None:
    store = InMemoryEventStore()
    repo = AggregateRepository(_account_aggregate(), store)
    await repo.append("acct-1", [_deposit(10)], expected_version=NO_STREAM)
    # Two writers both think the aggregate is at version 0.
    await repo.append("acct-1", [_deposit(5)], expected_version=0)
    with pytest.raises(OptimisticConcurrencyError):
        await repo.append("acct-1", [_deposit(7)], expected_version=0)


# --------------------------------------------------------------------------- #
# Snapshot acceleration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_snapshot_is_taken_on_cadence_and_used_on_load() -> None:
    store = InMemoryEventStore()
    agg = _account_aggregate()
    repo = AggregateRepository(
        agg, store, snapshots=store, snapshot_strategy=SnapshotStrategy(every=5)
    )

    version = NO_STREAM
    for _ in range(5):
        loaded = await repo.append("acct-1", [_deposit(10)], expected_version=version)
        version = loaded.version
    # After 5 events (versions 0..4) a snapshot at version 4 exists.
    snap = await store.load_latest("acct-1", snapshot_type="account")
    assert snap is not None
    assert snap.version == 4
    assert snap.state == {"balance": 50, "txns": 5}

    # A load now deserialises the snapshot and replays only the tail (none yet).
    loaded = await repo.load("acct-1")
    assert loaded.state == Account(balance=50, txns=5)


@pytest.mark.asyncio
async def test_load_replays_only_events_after_snapshot() -> None:
    store = InMemoryEventStore()
    agg = _account_aggregate()
    # Manually seed a snapshot at version 2 with a deliberately "wrong" balance so
    # we can prove the loader trusts the snapshot + only the tail (not a full replay).
    await store.append(
        "acct-1", [_deposit(10), _deposit(10), _deposit(10)], expected_version=NO_STREAM
    )
    from app.eventsourcing.store import Snapshot

    await store.save(
        Snapshot(stream_id="acct-1", version=2, state={"balance": 999, "txns": 3},
                 snapshot_type="account")
    )
    # Add one more event after the snapshot.
    await store.append("acct-1", [_deposit(1)], expected_version=2)

    repo = AggregateRepository(agg, store, snapshots=store)
    loaded = await repo.load("acct-1")
    # 999 (from snapshot) + 1 (the only replayed tail event) — proves no full replay.
    assert loaded.state.balance == 1000
    assert loaded.state.txns == 4
    assert loaded.version == 3


@pytest.mark.asyncio
async def test_snapshot_now_forces_a_snapshot() -> None:
    store = InMemoryEventStore()
    agg = _account_aggregate()
    repo = AggregateRepository(
        agg, store, snapshots=store, snapshot_strategy=SnapshotStrategy(every=1000)
    )
    await repo.append("acct-1", [_deposit(7)], expected_version=NO_STREAM)
    assert await store.load_latest("acct-1", snapshot_type="account") is None  # cadence not hit
    loaded = await repo.load("acct-1")
    await repo.snapshot_now(loaded)
    snap = await store.load_latest("acct-1", snapshot_type="account")
    assert snap is not None and snap.version == 0


@pytest.mark.asyncio
async def test_snapshot_now_without_store_raises() -> None:
    store = InMemoryEventStore()
    repo = AggregateRepository(_account_aggregate(), store)  # no snapshot store
    await repo.append("acct-1", [_deposit(1)], expected_version=NO_STREAM)
    loaded = await repo.load("acct-1")
    with pytest.raises(RuntimeError):
        await repo.snapshot_now(loaded)


# --------------------------------------------------------------------------- #
# Strategy cadence (pure)
# --------------------------------------------------------------------------- #


def test_snapshot_strategy_cadence() -> None:
    s = SnapshotStrategy(every=10)
    assert not s.should_snapshot(new_version=0)
    assert not s.should_snapshot(new_version=8)
    assert s.should_snapshot(new_version=9)  # 10 events accumulated (0..9)
    assert s.should_snapshot(new_version=19)
    # Far past the last snapshot also triggers (interval rule).
    assert s.should_snapshot(new_version=100, last_snapshot_version=50)
    assert not s.should_snapshot(new_version=55, last_snapshot_version=50)


def test_snapshot_strategy_rejects_bad_interval() -> None:
    with pytest.raises(ValueError):
        SnapshotStrategy(every=0)


def test_aggregate_repository_rejects_bad_read_batch() -> None:
    with pytest.raises(ValueError):
        AggregateRepository(_account_aggregate(), InMemoryEventStore(), read_batch=0)
