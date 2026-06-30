"""``ProviderGovernor`` — the governance layer above the round-1 video router.

This is the single object the scheduler/router holds. For every known provider it
owns a :class:`~app.video.governor.quota.QuotaAccountant`, a
:class:`~app.video.governor.throttle.ProviderThrottle`, a
:class:`~app.video.governor.sla.SlaTracker`, and a shared
:class:`~app.video.governor.fairshare.FairShareAllocator` across tenants. It exposes:

* :meth:`can_take` / :meth:`capacity` — read-only oracle answers (delegated), and
  :meth:`pick_provider` to choose the most routable backend across a set.
* :meth:`admit` — the *mutating* admission gate: it enforces fair-share ordering
  across tenants, reserves quota, consumes a pacing slot, and returns a
  :class:`RenderLease` the caller completes. Refusals come back as a lease with
  ``admitted=False`` and the reason — never an exception (the router decides what to
  do, e.g. degrade or try another provider).
* :meth:`complete` / lease context-manager — records the outcome: SLA sample,
  concurrency release, and (on a 429) a throttle backoff + breach event.

It does **not** call any provider and never flips ``KINORA_LIVE_VIDEO`` — it only
*decides and records*. Every breach/near-limit/recovery is emitted to the injected
:class:`~app.video.governor.events.GovernorEventBus`. Time is the injected clock
throughout, so the whole lifecycle is deterministic under a fake clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.logging import get_logger

from .clock import Clock, monotonic
from .config import GovernorConfig
from .events import EventCode, GovernorEvent, GovernorEventBus, Severity
from .fairshare import FairShareAllocator
from .oracle import CapacityOracle, CapacityVerdict, DenyReason, best_provider
from .quota import QuotaAccountant, RenderCost
from .sla import SlaSnapshot, SlaTracker
from .store import GovernorStore, InMemoryGovernorStore
from .throttle import ProviderThrottle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger("app.video.governor")


@dataclass
class _ProviderCell:
    """Everything the governor owns for a single provider."""

    name: str
    accountant: QuotaAccountant
    throttle: ProviderThrottle
    sla: SlaTracker
    oracle: CapacityOracle
    #: Quota near-limit fractions already alerted on (to fire each once per crossing).
    alerted_fractions: dict[str, float] = field(default_factory=dict)
    #: Last SLA grade we emitted a breach/recovery for (to avoid repeat spam).
    last_grade_breached: bool = False


@dataclass(frozen=True, slots=True)
class RenderLease:
    """The handle returned by :meth:`ProviderGovernor.admit`.

    When ``admitted`` is True the caller may submit the render and must call
    :meth:`ProviderGovernor.complete` (or use the :meth:`ProviderGovernor.lease`
    context manager) so concurrency is released and the SLA sample is recorded. When
    False, nothing was reserved and ``reason``/``verdict`` explain why.
    """

    provider: str
    admitted: bool
    cost: RenderCost
    tenant_id: str | None
    reason: DenyReason | None = None
    verdict: CapacityVerdict | None = None


class ProviderGovernor:
    """Govern admission + SLA/quota accounting across video providers."""

    def __init__(
        self,
        config: GovernorConfig,
        *,
        store: GovernorStore | None = None,
        events: GovernorEventBus | None = None,
        clock: Clock = monotonic,
    ) -> None:
        self._config = config
        self._store = store or InMemoryGovernorStore()
        self.events = events or GovernorEventBus()
        self._clock = clock
        self._cells: dict[str, _ProviderCell] = {}
        self._fairshare = FairShareAllocator(config.fairshare, clock=clock)

    # -- provider lifecycle ----------------------------------------------- #

    def ensure_provider(self, provider: str) -> _ProviderCell:
        """Lazily build (and cache) the governance cell for ``provider``."""
        cell = self._cells.get(provider)
        if cell is not None:
            return cell
        profile = self._config.profile_for(provider)
        accountant = QuotaAccountant(provider, profile.quota, self._store, clock=self._clock)
        throttle = ProviderThrottle(provider, profile.throttle, clock=self._clock)
        sla = SlaTracker(provider, profile.sla, clock=self._clock)
        oracle = CapacityOracle(provider, accountant, throttle, sla, clock=self._clock)
        cell = _ProviderCell(
            name=provider, accountant=accountant, throttle=throttle, sla=sla, oracle=oracle
        )
        self._cells[provider] = cell
        return cell

    def register_tenant(self, tenant_id: str, *, weight: float | None = None) -> None:
        """Set a tenant's fair-share weight (foreground reader > background book)."""
        self._fairshare.register(tenant_id, weight=weight)

    # -- read-only oracle surface ----------------------------------------- #

    async def can_take(self, provider: str, cost: RenderCost) -> CapacityVerdict:
        """"Can ``provider`` take a render of ``cost`` now? when free?" (read-only)."""
        return await self.ensure_provider(provider).oracle.can_take(cost)

    async def capacity(
        self, providers: Iterable[str], cost: RenderCost
    ) -> list[CapacityVerdict]:
        """Capacity verdicts for every provider in ``providers``."""
        return [await self.can_take(p, cost) for p in providers]

    async def pick_provider(
        self, providers: Iterable[str], cost: RenderCost
    ) -> CapacityVerdict | None:
        """The most routable provider for ``cost`` (None if the set is empty)."""
        return best_provider(await self.capacity(providers, cost))

    def sla(self, provider: str) -> SlaSnapshot:
        """The current SLA snapshot for ``provider``."""
        return self.ensure_provider(provider).sla.snapshot()

    # -- mutating admission ----------------------------------------------- #

    async def admit(
        self, provider: str, cost: RenderCost, *, tenant_id: str | None = None
    ) -> RenderLease:
        """Try to admit a render: fair-share → quota reserve → pacing slot.

        Returns a :class:`RenderLease`. On admission the quota counters and the
        concurrency gauge are advanced and a pacing slot is consumed; the caller
        must :meth:`complete` (or use :meth:`lease`). On refusal nothing is reserved.
        ``tenant_id`` enrols the submission in fair-share ordering so one book can't
        monopolise the provider — if another tenant is *starving* on this provider,
        this admission yields (returns a fair-share denial) so the scheduler retries
        the starving tenant first.

        Fair-share demand is *sticky* across capacity denials: a tenant whose admit
        is refused for quota/throttle keeps its one pending intent (idempotently, via
        :meth:`FairShareAllocator.ensure_demand`) so it accrues waiting age toward the
        starvation threshold. Demand is consumed only when the tenant is actually
        granted a slot.
        """
        cell = self.ensure_provider(provider)
        verdict = await cell.oracle.can_take(cost)

        # Fair-share gate: register (sticky) demand, and if some *other* tenant is
        # starving, defer to it (anti-starvation) — this tenant retries later.
        if tenant_id is not None:
            self._fairshare.ensure_demand(tenant_id, 1)
            decision = self._fairshare.next_tenant()
            if (
                decision.tenant_id is not None
                and decision.tenant_id != tenant_id
                and decision.starving
            ):
                self.events.emit(
                    GovernorEvent(
                        code=EventCode.FAIRSHARE_STARVATION,
                        severity=Severity.WARNING,
                        provider=provider,
                        message=f"deferring {tenant_id} to starving tenant {decision.tenant_id}",
                        at=self._clock(),
                        scope=decision.tenant_id,
                        detail={"yielded_by": tenant_id, "contenders": decision.contenders},
                    )
                )
                return RenderLease(
                    provider=provider,
                    admitted=False,
                    cost=cost,
                    tenant_id=tenant_id,
                    reason=None,
                    verdict=verdict,
                )

        if not verdict.admit:
            # Capacity denial: keep the tenant's sticky demand (it still wants the
            # slot and will retry) so its waiting age accrues toward starvation.
            self._emit_denial(provider, verdict)
            return RenderLease(
                provider=provider,
                admitted=False,
                cost=cost,
                tenant_id=tenant_id,
                reason=verdict.reason,
                verdict=verdict,
            )

        # Reserve quota; a race with a concurrent admit (gauge moved) can still flip
        # this to a refusal — honour it without recording (demand stays sticky).
        reservation = await cell.accountant.reserve(cost)
        if not reservation.admitted:
            self._emit_denial(provider, verdict, reason_override=DenyReason.QUOTA)
            return RenderLease(
                provider=provider,
                admitted=False,
                cost=cost,
                tenant_id=tenant_id,
                reason=DenyReason.QUOTA,
                verdict=verdict,
            )

        # Consume a pacing slot and pay down the fair-share grant (consumes demand).
        cell.throttle.acquire_delay()
        if tenant_id is not None:
            self._fairshare.grant(tenant_id)
        await self._check_near_limit(cell)
        logger.info(
            "governor.admitted",
            provider=provider,
            tenant=tenant_id,
            video_seconds=cost.video_seconds,
        )
        return RenderLease(
            provider=provider, admitted=True, cost=cost, tenant_id=tenant_id, verdict=verdict
        )

    async def complete(
        self,
        lease: RenderLease,
        *,
        success: bool,
        latency_ms: float = 0.0,
        rate_limited: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        """Record a render outcome and release its held capacity.

        - Releases the concurrency slot reserved at admit.
        - Feeds the SLA tracker (success+latency or failure).
        - On a 429 (``rate_limited``) applies a Retry-After/backoff park to the
          throttle and emits a ``THROTTLE_BACKOFF`` event; a clean success that
          clears a prior backoff emits ``THROTTLE_RECOVERED``.
        - Re-evaluates the SLA grade and emits breach/recovery transitions.

        Idempotency: completing a non-admitted lease is a no-op (nothing was held).
        """
        if not lease.admitted:
            return
        cell = self.ensure_provider(lease.provider)
        if lease.cost.concurrent:
            await cell.accountant.release(lease.cost.concurrent)

        if rate_limited:
            backoff = cell.throttle.note_rate_limited(retry_after_s=retry_after_s)
            cell.sla.record_failure(latency_ms)
            self.events.emit(
                GovernorEvent(
                    code=EventCode.THROTTLE_BACKOFF,
                    severity=Severity.WARNING,
                    provider=lease.provider,
                    message=f"rate-limited; backing off {backoff:.1f}s",
                    at=self._clock(),
                    observed=retry_after_s,
                    limit=backoff,
                    detail={"consecutive": cell.throttle.state().consecutive_throttles},
                )
            )
        elif success:
            recovered = cell.throttle.note_success()
            cell.sla.record_success(latency_ms)
            if recovered:
                self.events.emit(
                    GovernorEvent(
                        code=EventCode.THROTTLE_RECOVERED,
                        severity=Severity.INFO,
                        provider=lease.provider,
                        message="throttle recovered after backoff",
                        at=self._clock(),
                    )
                )
        else:
            cell.sla.record_failure(latency_ms)

        self._check_sla(cell)

    @asynccontextmanager
    async def lease(
        self, provider: str, cost: RenderCost, *, tenant_id: str | None = None
    ) -> AsyncIterator[RenderLease]:
        """Admit + auto-complete around a render body.

        Yields the :class:`RenderLease`. If the body raises, the lease is completed
        as a failure (releasing capacity); a normal exit completes as a success.
        Inside the body the caller may stash the real latency/429 on the lease via
        the returned object's fields — for finer control use :meth:`admit` /
        :meth:`complete` directly. The context manager is the simple, leak-proof path.
        """
        acquired = await self.admit(provider, cost, tenant_id=tenant_id)
        if not acquired.admitted:
            yield acquired
            return
        try:
            yield acquired
        except Exception:
            await self.complete(acquired, success=False)
            raise
        else:
            await self.complete(acquired, success=True)

    # -- alerting helpers ------------------------------------------------- #

    def _emit_denial(
        self,
        provider: str,
        verdict: CapacityVerdict,
        *,
        reason_override: DenyReason | None = None,
    ) -> None:
        # A denial is always a *hard* capacity block (quota/throttle); SLA breaches
        # are emitted separately by :meth:`_check_sla`, so an unhealthy-but-blocked
        # verdict still reports the concrete block here, not a duplicate breach.
        if reason_override is not None:
            reason = reason_override
        elif verdict.throttle_wait_s > 0:
            reason = DenyReason.THROTTLED
        else:
            reason = DenyReason.QUOTA
        if reason is DenyReason.THROTTLED:
            code, severity = EventCode.THROTTLE_BACKOFF, Severity.WARNING
        else:
            code, severity = EventCode.QUOTA_EXCEEDED, Severity.WARNING
        self.events.emit(
            GovernorEvent(
                code=code,
                severity=severity,
                provider=provider,
                message=f"submission denied: {reason.value}",
                at=self._clock(),
                observed=verdict.quota_utilisation,
                scope=reason.value,
            )
        )

    async def _check_near_limit(self, cell: _ProviderCell) -> None:
        """Emit a ``QUOTA_NEAR_LIMIT`` once per dimension per crossing."""
        for usage, fraction in await cell.accountant.near_limit():
            key = usage.dimension.value
            if cell.alerted_fractions.get(key) == fraction:
                continue
            cell.alerted_fractions[key] = fraction
            self.events.emit(
                GovernorEvent(
                    code=EventCode.QUOTA_NEAR_LIMIT,
                    severity=Severity.WARNING if fraction >= 0.9 else Severity.INFO,
                    provider=cell.name,
                    message=f"{key} at {usage.utilisation:.0%} of limit",
                    at=self._clock(),
                    observed=usage.used,
                    limit=usage.limit,
                    scope=key,
                )
            )

    def _check_sla(self, cell: _ProviderCell) -> None:
        """Emit SLA breach/recovery + error-budget-low transitions."""
        snap = cell.sla.snapshot()
        breached = snap.grade.rank >= 4  # F
        if breached and not cell.last_grade_breached:
            self.events.emit(
                GovernorEvent(
                    code=EventCode.SLA_BREACH,
                    severity=Severity.CRITICAL,
                    provider=cell.name,
                    message=f"SLA breached (grade {snap.grade.value}, "
                    f"burn {snap.error_budget_burn:.2f})",
                    at=self._clock(),
                    observed=snap.error_budget_burn,
                    limit=1.0,
                    detail=snap.as_log_fields(),
                )
            )
        elif not breached and cell.last_grade_breached:
            self.events.emit(
                GovernorEvent(
                    code=EventCode.SLA_RECOVERED,
                    severity=Severity.INFO,
                    provider=cell.name,
                    message=f"SLA recovered (grade {snap.grade.value})",
                    at=self._clock(),
                    detail=snap.as_log_fields(),
                )
            )
        elif (
            not breached
            and snap.error_budget_burn >= cell.sla.objective.burn_warning
            and snap.samples >= cell.sla.objective.min_samples
        ):
            self.events.emit(
                GovernorEvent(
                    code=EventCode.SLA_ERROR_BUDGET_LOW,
                    severity=Severity.WARNING,
                    provider=cell.name,
                    message=f"error budget burning ({snap.error_budget_burn:.0%})",
                    at=self._clock(),
                    observed=snap.error_budget_burn,
                    limit=1.0,
                )
            )
        cell.last_grade_breached = breached


__all__ = [
    "ProviderGovernor",
    "RenderLease",
]
