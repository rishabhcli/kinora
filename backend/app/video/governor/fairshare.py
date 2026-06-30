"""Weighted, anti-starvation fair-share of scarce provider capacity across tenants.

When several books/sessions all want renders from the same provider and that
provider has only ``N`` concurrent slots (or a thin requests-per-minute budget),
*who* gets the next slot matters. A naive FIFO lets one big book that enqueues a
thousand shots monopolise the provider and starve a reader who just opened page 1.

A *tenant* here is whatever scope fairness is enforced over — typically a session
or a book. Each tenant has a **weight** (a paying tier, or a foreground reader vs a
background pre-render, gets more); capacity is divided **in proportion to weight**
among tenants that currently *want* it. Two anti-starvation guarantees on top:

* **Deficit-weighted fair queuing.** Each contending tenant accrues a *deficit*
  proportional to its weight; the tenant with the largest deficit (most owed,
  relative to what it has received) is served next. Granting a slot pays down the
  deficit. This is the classic DRR/WFQ idea, made deterministic.
* **Starvation tier.** A tenant that has waited longer than ``starvation_age_s``
  without a grant is promoted to a *hard* priority tier above every non-starving
  tenant regardless of weight, so even a zero-weight/low-weight tenant is eventually
  served — no permanent starvation. Among equally-starving tenants the one served
  fewest times (then longest-waiting, then most-owed) goes first.

The allocator is pure in-memory state advanced by the injected clock; it does not
itself hold slots — the caller (the governor/oracle) asks :meth:`next_tenant` who
should get an available slot, then reports the grant. Concurrency *counts* are the
quota accountant's job; this only decides *ordering*.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock, monotonic


@dataclass(frozen=True, slots=True)
class FairShareConfig:
    """Tunables for :class:`FairShareAllocator`."""

    #: Default weight for a tenant registered without an explicit one.
    default_weight: float = 1.0
    #: A tenant that has been contending (ungranted) at least this long is
    #: considered *starving* and is served ahead of every non-starving tenant
    #: regardless of weight — the hard anti-starvation guarantee.
    starvation_age_s: float = 30.0

    def __post_init__(self) -> None:
        if self.default_weight <= 0:
            raise ValueError("default_weight must be > 0")
        if self.starvation_age_s <= 0:
            raise ValueError("starvation_age_s must be > 0")


@dataclass
class _Tenant:
    tenant_id: str
    weight: float
    #: Outstanding demand (slots the tenant currently wants).
    demand: int = 0
    #: Grants received in the current accounting epoch (drives the deficit).
    granted: int = 0
    #: Deficit counter — owed share minus received; higher ⇒ served sooner.
    deficit: float = 0.0
    #: When the tenant last *received* a grant (telemetry / least-recently-served).
    last_grant_at: float = 0.0
    #: When the tenant most recently started contending without a grant. ``None``
    #: means "not currently waiting"; a float (including ``0.0``) is a real instant.
    waiting_since: float | None = None


@dataclass(frozen=True, slots=True)
class FairShareDecision:
    """Why a tenant was (or was not) chosen for the next slot."""

    tenant_id: str | None
    priority: float
    starving: bool
    contenders: int


class FairShareAllocator:
    """Decide which contending tenant should receive the next provider slot.

    Lifecycle per tenant:

    * :meth:`register` (optional) sets a weight; otherwise the default applies on
      first :meth:`request`.
    * :meth:`request(tenant, n)` records demand (a tenant wanting ``n`` more slots).
    * :meth:`next_tenant` returns the starvation/deficit winner among contenders
      **without mutating** (a dry-run pick), or use :meth:`grant` to pick *and*
      record the grant (pays down deficit, decrements demand). :meth:`grant` returns
      the chosen tenant id (or ``None`` when nobody is contending).
    * :meth:`release` is a no-op hook for symmetry with the quota gauge — fairness
      tracks demand/grants, not live occupancy.
    """

    def __init__(
        self,
        config: FairShareConfig | None = None,
        *,
        clock: Clock = monotonic,
    ) -> None:
        self.config = config or FairShareConfig()
        self._clock = clock
        self._tenants: dict[str, _Tenant] = {}

    # -- registration ----------------------------------------------------- #

    def register(self, tenant_id: str, *, weight: float | None = None) -> None:
        """Register/update a tenant's weight (must be > 0)."""
        w = self.config.default_weight if weight is None else weight
        if w <= 0:
            raise ValueError("tenant weight must be > 0")
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            self._tenants[tenant_id] = _Tenant(tenant_id=tenant_id, weight=w)
        else:
            tenant.weight = w

    def _ensure(self, tenant_id: str) -> _Tenant:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            tenant = _Tenant(tenant_id=tenant_id, weight=self.config.default_weight)
            self._tenants[tenant_id] = tenant
        return tenant

    # -- demand ----------------------------------------------------------- #

    def request(self, tenant_id: str, count: int = 1) -> None:
        """Record that ``tenant_id`` wants ``count`` more slots (additive demand)."""
        if count <= 0:
            return
        tenant = self._ensure(tenant_id)
        was_idle = tenant.demand == 0
        tenant.demand += count
        if was_idle:
            tenant.waiting_since = self._clock()

    def ensure_demand(self, tenant_id: str, count: int = 1) -> None:
        """Ensure ``tenant_id`` has *at least* ``count`` outstanding demand.

        Idempotent across retries: unlike :meth:`request` (which is additive), this
        raises demand to ``count`` only if it is currently below it, so a tenant that
        repeatedly retries a capacity-denied admit doesn't inflate its demand — it
        keeps exactly one pending intent and goes on accruing waiting age toward the
        starvation threshold. The waiting clock starts when demand first appears.
        """
        if count <= 0:
            return
        tenant = self._ensure(tenant_id)
        was_idle = tenant.demand == 0
        tenant.demand = max(tenant.demand, count)
        if was_idle:
            tenant.waiting_since = self._clock()

    def withdraw(self, tenant_id: str, count: int = 1) -> None:
        """Drop ``count`` of a tenant's outstanding demand (e.g. a cancelled seek)."""
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            return
        tenant.demand = max(0, tenant.demand - count)
        if tenant.demand == 0:
            tenant.waiting_since = None

    # -- selection -------------------------------------------------------- #

    def _contenders(self) -> list[_Tenant]:
        return [t for t in self._tenants.values() if t.demand > 0]

    def _wait_age(self, tenant: _Tenant, now: float) -> float:
        """How long ``tenant`` has been contending without a grant (0 if not waiting)."""
        if tenant.waiting_since is None:
            return 0.0
        return max(0.0, now - tenant.waiting_since)

    def _is_starving(self, tenant: _Tenant, now: float) -> bool:
        return self._wait_age(tenant, now) >= self.config.starvation_age_s

    def _sort_key(
        self, tenant: _Tenant, now: float
    ) -> tuple[int, int, float, float, float, int]:
        """The selection key — higher sorts first (anti-starvation is a hard tier).

        ``(starving_tier, -granted_if_starving, wait_age, deficit, weight, -tiebreak)``:

        * **starving_tier** — any starving tenant (waited ≥ ``starvation_age_s``)
          outranks every non-starving one, so a light tenant that has waited past the
          threshold is served before a heavy tenant that has not: the hard
          anti-starvation guarantee.
        * **-granted (within the starving tier only)** — among starving tenants, the
          one that has received the *fewest* grants so far goes first, so a tenant
          that has never been served beats one that was served once even if their
          raw wait ages tie. Non-starving tenants share a neutral 0 here so normal
          (deficit-driven) weighted fairness is unaffected.
        * **wait_age / deficit / weight** — then longest-waiting, most-owed, heavier.
        * **-tiebreak** — a stable id hash so selection is fully deterministic.
        """
        starving = 1 if self._is_starving(tenant, now) else 0
        # Fewer grants ⇒ higher priority, but only as a starving-tier tiebreak; for
        # non-starving tenants this is a constant so weighted deficit ordering stands.
        served_penalty = -tenant.granted if starving else 0
        return (
            starving,
            served_penalty,
            self._wait_age(tenant, now),
            tenant.deficit,
            tenant.weight,
            -_stable(tenant.tenant_id),
        )

    def is_starving(self, tenant_id: str) -> bool:
        """True when ``tenant_id`` has contended past the starvation age."""
        tenant = self._tenants.get(tenant_id)
        if tenant is None or tenant.demand == 0:
            return False
        return self._is_starving(tenant, self._clock())

    def next_tenant(self) -> FairShareDecision:
        """Pick the next tenant to serve (dry-run; mutates nothing)."""
        now = self._clock()
        contenders = self._contenders()
        if not contenders:
            return FairShareDecision(None, priority=0.0, starving=False, contenders=0)
        best = max(contenders, key=lambda t: self._sort_key(t, now))
        return FairShareDecision(
            tenant_id=best.tenant_id,
            priority=best.deficit,
            starving=self._is_starving(best, now),
            contenders=len(contenders),
        )

    def grant(self, tenant_id: str | None = None) -> FairShareDecision:
        """Record a grant (deficit/demand accounting) for the next/given tenant.

        Before selecting, every contender accrues ``weight`` of deficit (so heavier
        tenants are owed more per round); the winner then pays down one round's worth
        of deficit (the total contending weight) and its demand drops by one. Over
        many rounds this makes grant shares converge to the weight ratios, while the
        starvation tier in the sort key guarantees a long-waiting light tenant is
        eventually served.

        When ``tenant_id`` is given the grant is recorded for *that* tenant (it is
        the one actually receiving the real provider slot, already selected by the
        caller); otherwise the deficit+starvation winner is chosen. A grant for a
        tenant with no demand is a no-op returning an empty decision.
        """
        now = self._clock()
        contenders = self._contenders()
        if not contenders:
            return FairShareDecision(None, priority=0.0, starving=False, contenders=0)

        total_weight = sum(t.weight for t in contenders)
        for tenant in contenders:
            tenant.deficit += tenant.weight

        if tenant_id is not None:
            winner = self._tenants.get(tenant_id)
            if winner is None or winner.demand <= 0:
                return FairShareDecision(None, priority=0.0, starving=False,
                                         contenders=len(contenders))
            decision = FairShareDecision(
                tenant_id=winner.tenant_id,
                priority=winner.deficit,
                starving=self._is_starving(winner, now),
                contenders=len(contenders),
            )
        else:
            decision = self.next_tenant()
            winner = self._tenants[decision.tenant_id]  # type: ignore[index]
        # Pay down: one round's worth of accrual so the deficit reflects (received −
        # owed); charging total_weight keeps long-run shares proportional to weights.
        winner.deficit -= total_weight
        winner.granted += 1
        winner.demand -= 1
        winner.last_grant_at = now
        winner.waiting_since = now if winner.demand > 0 else None
        return decision

    # -- inspection / maintenance ----------------------------------------- #

    def grants(self, tenant_id: str) -> int:
        tenant = self._tenants.get(tenant_id)
        return tenant.granted if tenant else 0

    def demand(self, tenant_id: str) -> int:
        tenant = self._tenants.get(tenant_id)
        return tenant.demand if tenant else 0

    def starving_tenants(self) -> list[str]:
        """All tenants currently past the starvation age (for alerting)."""
        return [t.tenant_id for t in self._contenders() if self.is_starving(t.tenant_id)]

    def reset_epoch(self) -> None:
        """Zero the per-epoch grant counters (keep weights/deficit/demand)."""
        for tenant in self._tenants.values():
            tenant.granted = 0


def _stable(tenant_id: str) -> int:
    """A deterministic tie-break integer from a tenant id (stable across runs)."""
    return int.from_bytes(tenant_id.encode("utf-8")[:8].ljust(8, b"\0"), "big")


__all__ = [
    "FairShareAllocator",
    "FairShareConfig",
    "FairShareDecision",
]
