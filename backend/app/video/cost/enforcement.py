"""Cross-provider budget enforcement: layered money caps + ``cheapest_capable``.

The scarce resource in kinora.md §11.1 is video-seconds, capped by the scheduler.
*This* layer caps the other axis — **money** — across heterogeneous providers, so
a cheap-per-second provider can't quietly blow the USD ceiling and an expensive
provider can't monopolize one book's allocation. Caps are layered, all in exact
:class:`~app.video.cost.money.Money`:

* a **global** cap (the §11.1 ~$30 USD ceiling, mirroring
  ``settings.budget_ceiling_usd``),
* optional **per-provider** caps (cap any one backend's share), and
* an optional **per-book** cap (no single adaptation drains the pool).

A reservation must clear *every* applicable cap; the first breached raises a
typed :class:`BudgetExceeded` naming the scope, the request, current outstanding
spend, and the cap — the same shape as the scheduler's
:class:`~app.memory.budget_service.BudgetExceeded`, so callers handle one idiom.

``BudgetEnforcer`` composes a :class:`~app.video.cost.ledger.SpendLedger`: it
checks the caps against current *outstanding* (reserved + committed) spend, then
reserves atomically. Because the in-memory ledger serializes reserve under a
lock and the enforcer checks-then-reserves *inside* that same awaited step, two
concurrent reservations that together breach a cap cannot both succeed.

``cheapest_capable`` ties the estimator to enforcement: given a canonical request
and the set of capability-eligible (provider, model) pairs, it returns the
cheapest option whose *high* estimate still fits under every cap — never an
optimistic point estimate, so a chosen provider is one we can actually afford.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.video.cost.estimator import ZERO_QUOTA, CostEstimate, CostEstimator, QuotaView
from app.video.cost.ledger import Reservation, SpendLedger, SpendScope
from app.video.cost.money import Currency, Money
from app.video.cost.request import VideoCostRequest


class BudgetExceeded(RuntimeError):  # noqa: N818 - public name in the task contract
    """Raised when a reservation would breach a money cap (typed, with context)."""

    def __init__(self, scope: str, *, requested: Money, outstanding: Money, cap: Money) -> None:
        self.scope = scope
        self.requested = requested
        self.outstanding = outstanding
        self.cap = cap
        super().__init__(
            f"video-cost {scope} cap exceeded: requested {requested} "
            f"+ outstanding {outstanding} > cap {cap}"
        )


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    """The layered money caps an enforcer applies (all one currency).

    Attributes:
        currency: The currency of every cap (and the ledger it backs).
        global_cap: The hard ceiling across all providers/books.
        per_provider: Optional per-provider caps (``provider -> Money``); a
            provider with no entry is bounded only by the global cap.
        per_book: An optional uniform cap any single ``book_id`` may consume.
        soft_cap_fraction: A 0..1 fraction of the global cap above which a
            reservation still succeeds but is flagged "soft-exceeded" (so the
            router can prefer degradation before the hard wall — §11.1 ``budget_low``).
    """

    currency: Currency
    global_cap: Money
    per_provider: dict[str, Money] = field(default_factory=dict)
    per_book: Money | None = None
    soft_cap_fraction: Decimal = Decimal("0.90")

    @classmethod
    def usd(
        cls,
        global_cap: Money,
        *,
        per_provider: dict[str, Money] | None = None,
        per_book: Money | None = None,
        soft_cap_fraction: Decimal = Decimal("0.90"),
    ) -> BudgetCaps:
        return cls(
            currency=Currency.USD,
            global_cap=global_cap,
            per_provider=dict(per_provider or {}),
            per_book=per_book,
            soft_cap_fraction=soft_cap_fraction,
        )

    def soft_global_cap(self) -> Money:
        return self.global_cap.scaled(self.soft_cap_fraction)


@dataclass(frozen=True, slots=True)
class AffordabilityCheck:
    """The result of a dry-run cap check for a single charge."""

    affordable: bool
    soft_exceeded: bool
    breached_scope: str | None = None
    cap: Money | None = None
    outstanding: Money | None = None


class BudgetEnforcer:
    """Layered money-cap enforcement over a :class:`SpendLedger`.

    The enforcer is the only writer of reservations in the cost layer — callers
    go through :meth:`reserve` so the cap check and the ledger write are one
    atomic awaited step (race-safe; see module docstring).
    """

    def __init__(self, ledger: SpendLedger, caps: BudgetCaps) -> None:
        self._ledger = ledger
        self._caps = caps

    @property
    def caps(self) -> BudgetCaps:
        return self._caps

    @property
    def ledger(self) -> SpendLedger:
        return self._ledger

    async def can_afford(self, amount: Money, scope: SpendScope) -> AffordabilityCheck:
        """Dry-run: would charging ``amount`` to ``scope`` clear every cap?

        Reads current outstanding (reserved+committed) totals; does not mutate.
        Note that under concurrency a passing dry-run can still lose the race at
        :meth:`reserve` time — :meth:`reserve` re-checks atomically.
        """
        self._check_currency(amount)

        out_global = await self._ledger.outstanding()
        if out_global + amount > self._caps.global_cap:
            return AffordabilityCheck(
                affordable=False,
                soft_exceeded=True,
                breached_scope="global",
                cap=self._caps.global_cap,
                outstanding=out_global,
            )

        provider_cap = self._caps.per_provider.get(scope.provider.lower())
        if provider_cap is not None:
            out_provider = await self._ledger.outstanding(provider=scope.provider)
            if out_provider + amount > provider_cap:
                return AffordabilityCheck(
                    affordable=False,
                    soft_exceeded=False,
                    breached_scope=f"provider:{scope.provider}",
                    cap=provider_cap,
                    outstanding=out_provider,
                )

        if self._caps.per_book is not None and scope.book_id is not None:
            out_book = await self._book_outstanding(scope.book_id)
            if out_book + amount > self._caps.per_book:
                return AffordabilityCheck(
                    affordable=False,
                    soft_exceeded=False,
                    breached_scope=f"book:{scope.book_id}",
                    cap=self._caps.per_book,
                    outstanding=out_book,
                )

        soft = out_global + amount > self._caps.soft_global_cap()
        return AffordabilityCheck(affordable=True, soft_exceeded=soft)

    async def reserve(self, amount: Money, scope: SpendScope) -> Reservation:
        """Atomically check every cap then earmark ``amount``; raise if any breached."""
        check = await self.can_afford(amount, scope)
        if not check.affordable:
            assert check.breached_scope is not None  # noqa: S101 - invariant of !affordable
            raise BudgetExceeded(
                check.breached_scope,
                requested=amount,
                outstanding=check.outstanding or Money.zero(self._caps.currency),
                cap=check.cap or self._caps.global_cap,
            )
        return await self._ledger.reserve(amount, scope)

    async def commit(self, reservation: Reservation, actual: Money | None = None) -> Reservation:
        """Commit a reservation at its actual billed amount (delegates to the ledger)."""
        return await self._ledger.commit(reservation, actual)

    async def release(self, reservation: Reservation) -> Reservation:
        """Release an un-committed reservation (delegates to the ledger)."""
        return await self._ledger.release(reservation)

    async def remaining_global(self) -> Money:
        """Money left under the hard global cap right now (never negative)."""
        out = await self._ledger.outstanding()
        if out >= self._caps.global_cap:
            return Money.zero(self._caps.currency)
        return self._caps.global_cap - out

    def _check_currency(self, amount: Money) -> None:
        if amount.currency is not self._caps.currency:
            from app.video.cost.money import CurrencyMismatch

            raise CurrencyMismatch(self._caps.currency, amount.currency)

    async def _book_outstanding(self, book_id: str) -> Money:
        """Outstanding (reserved+committed) money attributed to one book.

        The ledger aggregates per provider natively; per-book needs the full scope
        breakdown. The in-memory ledger exposes :meth:`committed_scopes`; for the
        general protocol we sum across providers' scope detail when available,
        else fall back to global (over-conservative, never under-counts).
        """
        committed_scopes = getattr(self._ledger, "committed_scopes", None)
        reserved_scopes = getattr(self._ledger, "reserved_scopes", None)
        if committed_scopes is None:
            # Protocol-only ledger without scope detail → conservative global bound.
            return await self._ledger.outstanding()
        total = Money.zero(self._caps.currency)
        for (_, b, _), money in (await committed_scopes()).items():
            if b == book_id:
                total = total + money
        if reserved_scopes is not None:
            for (_, b, _), money in (await reserved_scopes()).items():
                if b == book_id:
                    total = total + money
        return total


# --------------------------------------------------------------------------- #
# cheapest_capable — the router's one-call entry point
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    """The chosen provider/model plus the estimate that justified it."""

    provider: str
    model: str
    estimate: CostEstimate
    soft_exceeded: bool


@dataclass(frozen=True, slots=True)
class CapabilityCandidate:
    """A capability-eligible provider/model the router is willing to use.

    Capability filtering (does this provider support the requested mode /
    resolution at all?) happens *before* cost — the cost layer only ranks the set
    the router already deemed capable, so it never picks a provider that can't do
    the job just because it's cheap.
    """

    provider: str
    model: str


async def cheapest_capable(
    request: VideoCostRequest,
    candidates: list[CapabilityCandidate],
    *,
    estimator: CostEstimator,
    enforcer: BudgetEnforcer,
    quota: QuotaView = ZERO_QUOTA,
    fail_on_empty: bool = False,
) -> ProviderChoice | None:
    """Return the cheapest capable provider whose worst-case cost still fits the caps.

    Ranking rule:

    1. Price each candidate (marginal cost, free-tier aware).
    2. Discard any whose **high** (worst-case) estimate would breach a cap — we
       commit on the pessimistic bound so a chosen provider is genuinely affordable.
    3. Among the survivors pick the lowest **expected** cost; break ties by higher
       confidence, then by lower **high** (tighter worst case), then provider name
       for total determinism.

    Returns ``None`` when nothing is both capable and affordable (or raises a
    :class:`BudgetExceeded` for the cheapest option when ``fail_on_empty`` is set,
    so the caller gets a typed reason rather than a silent ``None``).
    """
    scored: list[tuple[CostEstimate, AffordabilityCheck, CapabilityCandidate]] = []
    for cand in candidates:
        if not estimator.registry.has(cand.provider, cand.model):
            continue
        est = estimator.estimate(request, cand.provider, cand.model, quota=quota)
        scope = SpendScope(
            provider=cand.provider, book_id=request.book_id, session_id=request.session_id
        )
        # Worst-case: check the HIGH estimate against the caps.
        check = await enforcer.can_afford(est.high, scope)
        scored.append((est, check, cand))

    affordable = [(e, c, cand) for (e, c, cand) in scored if c.affordable]
    if not affordable:
        if fail_on_empty and scored:
            cheapest = min(scored, key=lambda t: (t[0].expected.units, -t[0].confidence))
            est, check, cand = cheapest
            scope = SpendScope(
                provider=cand.provider, book_id=request.book_id, session_id=request.session_id
            )
            raise BudgetExceeded(
                check.breached_scope or "global",
                requested=est.high,
                outstanding=check.outstanding or Money.zero(est.currency),
                cap=check.cap or enforcer.caps.global_cap,
            )
        return None

    best_est, best_check, best_cand = min(
        affordable,
        key=lambda t: (t[0].expected.units, -t[0].confidence, t[0].high.units, t[2].provider),
    )
    return ProviderChoice(
        provider=best_cand.provider,
        model=best_cand.model,
        estimate=best_est,
        soft_exceeded=best_check.soft_exceeded,
    )


__all__ = [
    "AffordabilityCheck",
    "BudgetCaps",
    "BudgetEnforcer",
    "BudgetExceeded",
    "CapabilityCandidate",
    "ProviderChoice",
    "cheapest_capable",
]
