"""Unit tests for the two-phase SpendLedger (in-memory + Redis-transport fake).

Covers reserve->commit->release lifecycle, illegal transitions, per-provider /
per-scope aggregation, commit-at-actual (the estimate-vs-bill difference), and the
Redis-backed ledger over a deterministic in-memory hash fake. The reservation
race-safety test lives in the enforcement suite (caps are what races).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from app.video.cost.ledger import (
    InMemorySpendLedger,
    LedgerError,
    RedisSpendLedger,
    ReservationState,
    SpendScope,
)
from app.video.cost.money import Currency, CurrencyMismatch, Money


async def test_reserve_commit_release_lifecycle() -> None:
    ledger = InMemorySpendLedger()
    scope = SpendScope(provider="minimax", book_id="b1", session_id="s1")
    res = await ledger.reserve(Money.usd("0.19"), scope)
    assert res.state is ReservationState.RESERVED
    assert (await ledger.reserved_total()) == Money.usd("0.19")
    assert (await ledger.committed_total()) == Money.usd("0")
    assert (await ledger.outstanding()) == Money.usd("0.19")

    committed = await ledger.commit(res)
    assert committed.state is ReservationState.COMMITTED
    assert (await ledger.reserved_total()) == Money.usd("0")
    assert (await ledger.committed_total()) == Money.usd("0.19")
    assert (await ledger.outstanding()) == Money.usd("0.19")


async def test_release_returns_earmark() -> None:
    ledger = InMemorySpendLedger()
    res = await ledger.reserve(Money.usd("0.50"), SpendScope(provider="dashscope"))
    released = await ledger.release(res)
    assert released.state is ReservationState.RELEASED
    assert (await ledger.outstanding()) == Money.usd("0")
    # Releasing twice is idempotent.
    assert (await ledger.release(res)).state is ReservationState.RELEASED


async def test_commit_at_actual_differs_from_estimate() -> None:
    ledger = InMemorySpendLedger()
    res = await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    # Provider billed more than estimated.
    committed = await ledger.commit(res, Money.usd("0.21"))
    assert committed.amount == Money.usd("0.21")
    assert (await ledger.committed_total()) == Money.usd("0.21")
    assert (await ledger.reserved_total()) == Money.usd("0")


async def test_illegal_transitions() -> None:
    ledger = InMemorySpendLedger()
    res = await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    await ledger.commit(res)
    with pytest.raises(LedgerError):
        await ledger.commit(res)  # already committed
    with pytest.raises(LedgerError):
        await ledger.release(res)  # cannot release committed


async def test_unknown_reservation_errors() -> None:
    ledger = InMemorySpendLedger()
    from app.video.cost.ledger import Reservation

    ghost = Reservation(id="nope", amount=Money.usd("1"), scope=SpendScope(provider="x"))
    with pytest.raises(LedgerError):
        await ledger.commit(ghost)


async def test_negative_and_currency_guards() -> None:
    ledger = InMemorySpendLedger()
    with pytest.raises(ValueError):
        await ledger.reserve(Money.usd("-1"), SpendScope(provider="x"))
    with pytest.raises(CurrencyMismatch):
        await ledger.reserve(Money.from_decimal("1", Currency.EUR), SpendScope(provider="x"))


async def test_per_provider_aggregation() -> None:
    ledger = InMemorySpendLedger()
    await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    r2 = await ledger.reserve(Money.usd("0.60"), SpendScope(provider="dashscope"))
    await ledger.commit(r2)
    assert (await ledger.outstanding(provider="minimax")) == Money.usd("0.19")
    assert (await ledger.outstanding(provider="dashscope")) == Money.usd("0.60")
    assert (await ledger.outstanding()) == Money.usd("0.79")

    by = await ledger.by_provider()
    assert by["minimax"].reserved == Money.usd("0.19")
    assert by["minimax"].committed == Money.usd("0")
    assert by["dashscope"].committed == Money.usd("0.60")
    assert by["dashscope"].outstanding == Money.usd("0.60")


async def test_committed_and_reserved_scopes() -> None:
    ledger = InMemorySpendLedger()
    r1 = await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))
    await ledger.commit(r1)
    await ledger.reserve(Money.usd("0.10"), SpendScope(provider="dashscope", book_id="b1"))
    committed = await ledger.committed_scopes()
    reserved = await ledger.reserved_scopes()
    assert committed[("minimax", "b1", None)] == Money.usd("0.19")
    assert reserved[("dashscope", "b1", None)] == Money.usd("0.10")


# --------------------------------------------------------------------------- #
# Redis-backed ledger over a deterministic hash fake
# --------------------------------------------------------------------------- #


class FakeRedisHash:
    """A minimal, deterministic RedisLedgerTransport (integer hash fields)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, int]] = {}

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        h = self.store.setdefault(key, {})
        h[field] = h.get(field, 0) + amount
        return h[field]

    async def hget(self, key: str, field: str) -> int | None:
        return self.store.get(key, {}).get(field)

    async def hgetall(self, key: str) -> Mapping[str, int]:
        return dict(self.store.get(key, {}))


async def test_redis_ledger_mirrors_inmemory_semantics() -> None:
    transport = FakeRedisHash()
    ledger = RedisSpendLedger(transport=transport)
    res = await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))
    assert (await ledger.reserved_total()) == Money.usd("0.19")
    assert (await ledger.outstanding(provider="minimax")) == Money.usd("0.19")

    committed = await ledger.commit(res, Money.usd("0.21"))
    assert committed.state is ReservationState.COMMITTED
    # Earmark moved out of reserved; billed amount in committed.
    assert (await ledger.reserved_total()) == Money.usd("0")
    assert (await ledger.committed_total()) == Money.usd("0.21")
    assert (await ledger.outstanding()) == Money.usd("0.21")

    by = await ledger.by_provider()
    assert by["minimax"].committed == Money.usd("0.21")


async def test_redis_ledger_release() -> None:
    ledger = RedisSpendLedger(transport=FakeRedisHash())
    res = await ledger.reserve(Money.usd("0.50"), SpendScope(provider="dashscope"))
    await ledger.release(res)
    assert (await ledger.outstanding()) == Money.usd("0")


async def test_redis_ledger_stores_integer_minor_units_no_float() -> None:
    transport = FakeRedisHash()
    ledger = RedisSpendLedger(transport=transport)
    await ledger.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    # The raw stored value is an int (Money.units), never a float.
    raw = transport.store["kinora:video:cost:reserved"]["minimax|-|-"]
    assert isinstance(raw, int)
    assert raw == Money.usd("0.19").units


async def test_inmemory_ids_are_unique_under_concurrency() -> None:
    ledger = InMemorySpendLedger()
    scope = SpendScope(provider="minimax")
    reservations = await asyncio.gather(
        *(ledger.reserve(Money.usd("0.01"), scope) for _ in range(50))
    )
    assert len({r.id for r in reservations}) == 50
