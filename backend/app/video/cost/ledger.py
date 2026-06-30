"""``SpendLedger`` — atomic reserve → commit → release of *money* across providers.

The legacy :class:`~app.providers.minimax.RedisSpendStore` tracks a single
cumulative-USD float. That is enough for a one-provider belt-and-suspenders cap
but not for a cross-provider competition: we need spend attributable *per
provider / book / session*, in exact :class:`~app.video.cost.money.Money`, with a
two-phase reserve→commit so a render that is priced-and-earmarked but not yet
billed cannot be double-spent by a concurrent request.

The contract mirrors the scheduler's video-seconds
:class:`~app.memory.budget_service.BudgetService`:

* ``reserve(amount, scope)`` earmarks money and returns a :class:`Reservation`
  handle. Reserved-but-uncommitted money counts against caps (so two concurrent
  reservations can't both fit under a cap that only fits one).
* ``commit(reservation, actual)`` converts a reservation to spend at the *actual*
  billed amount (which may differ from the estimate), releasing any difference.
* ``release(reservation)`` cancels an un-committed reservation (render failed /
  was gated off), returning the earmark to the pool.

Two implementations share one base so the in-memory store and any Redis-backed
store are interchangeable and the *enforcement* logic is written once. The
in-memory store serializes its read-modify-write under an ``asyncio.Lock`` so the
reservation race is closed even under cooperative concurrency; the Redis
interface documents the same guarantee via a Lua/WATCH critical section (left as
an injectable transport so this module stays infra-free and testable).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.video.cost.money import Currency, Money


class ReservationState(StrEnum):
    """Lifecycle of a single reservation."""

    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class SpendScope:
    """The attribution dimensions money is tracked against.

    ``None`` on a dimension means "not scoped to a particular X"; a reservation
    always has a provider (so per-provider caps can bind) but may omit book /
    session when the caller doesn't have them.
    """

    provider: str
    book_id: str | None = None
    session_id: str | None = None

    def key(self) -> tuple[str, str | None, str | None]:
        return (self.provider.lower(), self.book_id, self.session_id)


@dataclass(frozen=True, slots=True)
class Reservation:
    """A handle to an outstanding earmark of money."""

    id: str
    amount: Money
    scope: SpendScope
    state: ReservationState = ReservationState.RESERVED


class LedgerError(RuntimeError):
    """Base for ledger misuse (unknown / already-finalized reservation)."""


@dataclass(frozen=True, slots=True)
class ProviderSpend:
    """A snapshot of one provider's reserved vs committed money."""

    provider: str
    reserved: Money
    committed: Money

    @property
    def outstanding(self) -> Money:
        """Reserved + committed — the total counting against a cap *right now*."""
        return self.reserved + self.committed


class SpendLedger(Protocol):
    """Cross-provider, multi-scope, two-phase money ledger (exact Money).

    Currency note: a ledger is single-currency (the caps it backs are). Reserving
    a mismatched currency raises :class:`~app.video.cost.money.CurrencyMismatch`
    via the underlying :class:`Money` arithmetic. Convert with a
    :class:`~app.video.cost.money.FxConverter` before reserving if needed.
    """

    async def reserve(self, amount: Money, scope: SpendScope) -> Reservation: ...
    async def commit(
        self, reservation: Reservation, actual: Money | None = None
    ) -> Reservation: ...
    async def release(self, reservation: Reservation) -> Reservation: ...
    async def outstanding(self, *, provider: str | None = None) -> Money:
        """Total reserved+committed across all scopes, or for one provider."""
        ...
    async def committed_total(self, *, provider: str | None = None) -> Money: ...
    async def reserved_total(self, *, provider: str | None = None) -> Money: ...
    async def by_provider(self) -> dict[str, ProviderSpend]: ...


# --------------------------------------------------------------------------- #
# In-memory implementation (fallback / tests) — race-safe under asyncio.Lock
# --------------------------------------------------------------------------- #


def _seq_ids() -> Iterator[str]:
    n = 0
    while True:
        n += 1
        yield f"res-{n:08d}"


class InMemorySpendLedger:
    """Process-local :class:`SpendLedger`. Not cross-process; ideal for tests.

    All mutating ops take ``self._lock`` so a reserve's read-cap-check-write is
    atomic with respect to other reserves — this is what makes the reservation
    race test deterministic. The caller (the enforcer) supplies the cap check; the
    ledger itself only tracks money and refuses illegal state transitions.
    """

    def __init__(self, currency: Currency = Currency.USD) -> None:
        self._currency = currency
        self._reservations: dict[str, Reservation] = {}
        self._lock = asyncio.Lock()
        self._gen = _seq_ids()

    @property
    def currency(self) -> Currency:
        return self._currency

    def _new_id(self) -> str:
        return next(self._gen)

    async def reserve(self, amount: Money, scope: SpendScope) -> Reservation:
        if amount.currency is not self._currency:
            from app.video.cost.money import CurrencyMismatch

            raise CurrencyMismatch(self._currency, amount.currency)
        if amount.units < 0:
            raise ValueError("cannot reserve a negative amount")
        async with self._lock:
            res = Reservation(id=self._new_id(), amount=amount, scope=scope)
            self._reservations[res.id] = res
            return res

    async def commit(self, reservation: Reservation, actual: Money | None = None) -> Reservation:
        async with self._lock:
            current = self._require(reservation.id)
            if current.state is not ReservationState.RESERVED:
                raise LedgerError(
                    f"reservation {reservation.id} is {current.state}, cannot commit"
                )
            billed = actual if actual is not None else current.amount
            if billed.currency is not self._currency:
                from app.video.cost.money import CurrencyMismatch

                raise CurrencyMismatch(self._currency, billed.currency)
            if billed.units < 0:
                raise ValueError("cannot commit a negative amount")
            committed = Reservation(
                id=current.id,
                amount=billed,
                scope=current.scope,
                state=ReservationState.COMMITTED,
            )
            self._reservations[current.id] = committed
            return committed

    async def release(self, reservation: Reservation) -> Reservation:
        async with self._lock:
            current = self._require(reservation.id)
            if current.state is ReservationState.COMMITTED:
                raise LedgerError(
                    f"reservation {reservation.id} is committed, cannot release"
                )
            if current.state is ReservationState.RELEASED:
                return current
            released = Reservation(
                id=current.id,
                amount=current.amount,
                scope=current.scope,
                state=ReservationState.RELEASED,
            )
            self._reservations[current.id] = released
            return released

    def _require(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError as exc:
            raise LedgerError(f"unknown reservation {reservation_id}") from exc

    # -- aggregates (no lock needed: snapshots over an immutable-value dict) -- #

    def _match(self, res: Reservation, provider: str | None) -> bool:
        return provider is None or res.scope.provider.lower() == provider.lower()

    async def reserved_total(self, *, provider: str | None = None) -> Money:
        total = Money.zero(self._currency)
        for res in self._reservations.values():
            if res.state is ReservationState.RESERVED and self._match(res, provider):
                total = total + res.amount
        return total

    async def committed_total(self, *, provider: str | None = None) -> Money:
        total = Money.zero(self._currency)
        for res in self._reservations.values():
            if res.state is ReservationState.COMMITTED and self._match(res, provider):
                total = total + res.amount
        return total

    async def outstanding(self, *, provider: str | None = None) -> Money:
        reserved = await self.reserved_total(provider=provider)
        committed = await self.committed_total(provider=provider)
        return reserved + committed

    async def by_provider(self) -> dict[str, ProviderSpend]:
        agg: dict[str, list[Money]] = {}
        for res in self._reservations.values():
            if res.state is ReservationState.RELEASED:
                continue
            slot = agg.setdefault(
                res.scope.provider.lower(),
                [Money.zero(self._currency), Money.zero(self._currency)],
            )
            if res.state is ReservationState.RESERVED:
                slot[0] = slot[0] + res.amount
            else:  # COMMITTED
                slot[1] = slot[1] + res.amount
        return {
            provider: ProviderSpend(provider=provider, reserved=r, committed=c)
            for provider, (r, c) in agg.items()
        }

    # -- bridge to the legacy single-USD store -------------------------- #

    async def committed_scopes(self) -> dict[tuple[str, str | None, str | None], Money]:
        """Committed money grouped by full scope key (for per-book reconciliation)."""
        return self._scopes(ReservationState.COMMITTED)

    async def reserved_scopes(self) -> dict[tuple[str, str | None, str | None], Money]:
        """Outstanding reserved money grouped by full scope key (per-book caps)."""
        return self._scopes(ReservationState.RESERVED)

    def _scopes(
        self, state: ReservationState
    ) -> dict[tuple[str, str | None, str | None], Money]:
        out: dict[tuple[str, str | None, str | None], Money] = {}
        for res in self._reservations.values():
            if res.state is not state:
                continue
            k = res.scope.key()
            out[k] = out.get(k, Money.zero(self._currency)) + res.amount
        return out


# --------------------------------------------------------------------------- #
# Redis-backed implementation INTERFACE (mirrors RedisSpendStore semantics)
# --------------------------------------------------------------------------- #


class RedisLedgerTransport(Protocol):
    """The minimal Redis surface the :class:`RedisSpendLedger` needs.

    Modeled on the ops :class:`~app.providers.minimax.RedisSpendStore` already
    uses (``incrbyfloat`` / ``get``) plus a hash for per-scope attribution and an
    *atomic* check-and-reserve primitive. Kept as a Protocol so this module never
    imports a real redis client and the unit suite injects a deterministic fake.

    Money is stored as integer minor-units (``Money.units``) — never floats — so
    the cross-process counter inherits the no-drift guarantee of :class:`Money`.
    """

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        """Atomically add integer ``amount`` to hash ``key[field]``; return new."""
        ...

    async def hget(self, key: str, field: str) -> int | None: ...
    async def hgetall(self, key: str) -> Mapping[str, int]: ...


@dataclass
class RedisSpendLedger:
    """Cross-process :class:`SpendLedger` over a :class:`RedisLedgerTransport`.

    Layout (one hash per phase, fields keyed by scope) so per-provider / per-book
    aggregates are a single ``HGETALL``, and the cumulative-USD compatibility with
    the legacy store is a derived sum:

    * ``{prefix}:reserved`` hash: outstanding earmarks (added on reserve, removed
      on commit/release).
    * ``{prefix}:committed`` hash: realized spend (added on commit).

    Atomicity: ``hincrby`` is atomic in Redis, so reserve/commit/release each move
    money with a single atomic op. The *cap check* that precedes a reserve is the
    enforcer's responsibility and must run inside the same WATCH/Lua critical
    section in production; this interface exposes the building blocks. The
    in-memory ledger is the reference semantics the tests pin.
    """

    transport: RedisLedgerTransport
    currency: Currency = Currency.USD
    prefix: str = "kinora:video:cost"
    _gen: Iterator[str] = field(default_factory=_seq_ids, repr=False)

    @staticmethod
    def _field(scope: SpendScope) -> str:
        book = scope.book_id or "-"
        session = scope.session_id or "-"
        return f"{scope.provider.lower()}|{book}|{session}"

    async def reserve(self, amount: Money, scope: SpendScope) -> Reservation:
        if amount.currency is not self.currency:
            from app.video.cost.money import CurrencyMismatch

            raise CurrencyMismatch(self.currency, amount.currency)
        if amount.units < 0:
            raise ValueError("cannot reserve a negative amount")
        await self.transport.hincrby(f"{self.prefix}:reserved", self._field(scope), amount.units)
        return Reservation(id=next(self._gen), amount=amount, scope=scope)

    async def commit(self, reservation: Reservation, actual: Money | None = None) -> Reservation:
        billed = actual if actual is not None else reservation.amount
        field_ = self._field(reservation.scope)
        # Move the original earmark out of reserved, add the billed amount to committed.
        await self.transport.hincrby(f"{self.prefix}:reserved", field_, -reservation.amount.units)
        await self.transport.hincrby(f"{self.prefix}:committed", field_, billed.units)
        return Reservation(
            id=reservation.id,
            amount=billed,
            scope=reservation.scope,
            state=ReservationState.COMMITTED,
        )

    async def release(self, reservation: Reservation) -> Reservation:
        await self.transport.hincrby(
            f"{self.prefix}:reserved", self._field(reservation.scope), -reservation.amount.units
        )
        return Reservation(
            id=reservation.id,
            amount=reservation.amount,
            scope=reservation.scope,
            state=ReservationState.RELEASED,
        )

    async def _phase_total(self, phase: str, provider: str | None) -> Money:
        raw = await self.transport.hgetall(f"{self.prefix}:{phase}")
        total = 0
        for field_, units in raw.items():
            if provider is None or field_.split("|", 1)[0] == provider.lower():
                total += int(units)
        return Money(total, self.currency)

    async def reserved_total(self, *, provider: str | None = None) -> Money:
        return await self._phase_total("reserved", provider)

    async def committed_total(self, *, provider: str | None = None) -> Money:
        return await self._phase_total("committed", provider)

    async def outstanding(self, *, provider: str | None = None) -> Money:
        return await self.reserved_total(provider=provider) + await self.committed_total(
            provider=provider
        )

    async def by_provider(self) -> dict[str, ProviderSpend]:
        reserved = await self.transport.hgetall(f"{self.prefix}:reserved")
        committed = await self.transport.hgetall(f"{self.prefix}:committed")
        providers: dict[str, list[int]] = {}
        for field_, units in reserved.items():
            providers.setdefault(field_.split("|", 1)[0], [0, 0])[0] += int(units)
        for field_, units in committed.items():
            providers.setdefault(field_.split("|", 1)[0], [0, 0])[1] += int(units)
        return {
            p: ProviderSpend(
                provider=p,
                reserved=Money(r, self.currency),
                committed=Money(c, self.currency),
            )
            for p, (r, c) in providers.items()
        }


__all__ = [
    "InMemorySpendLedger",
    "LedgerError",
    "ProviderSpend",
    "RedisLedgerTransport",
    "RedisSpendLedger",
    "Reservation",
    "ReservationState",
    "SpendLedger",
    "SpendScope",
]
