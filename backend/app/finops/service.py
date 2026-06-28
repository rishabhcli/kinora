"""FinOpsService — the I/O aggregate that fronts budget & cost governance.

It composes the load-bearing :class:`~app.memory.budget_service.BudgetService`
(reserve/commit/release over ``budget_ledger`` — **unchanged**) with the FinOps
additions:

* a **tenant cap** layered on top of the global/session/scene caps the budget
  service already enforces — checked under the *same* advisory lock so two
  reservations cannot both slip past it (kinora.md §11.1);
* the **USD cost ledger** (write a valued spend row; read scoped USD/attribution);
* **tier evaluation** (soft/hard/floor alerts per scope) and a **governance
  decision** (forecast + optimizer + recommendation) for a live reading session;
* **reconciliation** of the cost ledger's video-seconds against the authoritative
  budget ledger.

Bound to one request/unit-of-work :class:`AsyncSession` (like every repo); the
container builds one per request. The reserve/commit/release passthroughs keep
the exact signatures the scheduler and render pipeline call, so wiring this in is
non-breaking.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings
from app.db.repositories.book import BookRepo
from app.db.repositories.budget import BudgetRepo
from app.db.repositories.finops import CostLedgerRepo, micros_to_usd
from app.finops.attribution import attribute_agent
from app.finops.forecast import ReadingTrajectory
from app.finops.governor import GovernanceDecision, govern
from app.finops.ledger import CostSummary, Reconciliation, reconcile
from app.finops.optimizer import ShotOption
from app.finops.tiers import (
    AlertLevel,
    BudgetScopeKind,
    BudgetTierPolicy,
    CapStatus,
)
from app.memory.budget_service import (
    BudgetExceeded,
    BudgetLimits,
    BudgetService,
    Reservation,
)
from app.providers.types import Usage

# The FinOps tenant-cap check shares the budget domain's advisory-lock key so a
# tenant reservation serializes against every other reservation on the same DB.
_LOCK_KEY = int.from_bytes(hashlib.sha1(b"kinora:budget").digest()[:8], "big", signed=True)


@dataclass(frozen=True, slots=True)
class TenantUsage:
    """A tenant's video-seconds usage against its cap."""

    tenant_id: str
    used_s: float
    status: CapStatus


class FinOpsService:
    """Budget & cost governance for one unit of work (kinora.md §11.1)."""

    def __init__(
        self,
        *,
        budget_repo: BudgetRepo,
        cost_repo: CostLedgerRepo,
        book_repo: BookRepo,
        limits: BudgetLimits,
        policy: BudgetTierPolicy,
        settings: Settings | None = None,
    ) -> None:
        self._budget_repo = budget_repo
        self._cost_repo = cost_repo
        self._book_repo = book_repo
        self._limits = limits
        self._policy = policy
        self._settings = settings
        # The wrapped, contract-preserving video-seconds guardrail.
        self._budget = BudgetService(repo=budget_repo, limits=limits)

    @property
    def budget(self) -> BudgetService:
        """The wrapped :class:`BudgetService` (the reserve/commit/release authority)."""
        return self._budget

    @property
    def policy(self) -> BudgetTierPolicy:
        return self._policy

    # -- reserve / commit / release passthroughs (unchanged contract) -------- #

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        tenant_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        """Reserve video-seconds across global/tenant/session/scene caps.

        Delegates the global/session/scene checks (and the advisory lock + ledger
        append) to :class:`BudgetService`; *additionally* enforces the per-tenant
        cap under the same lock so the tenant total cannot be raced past its
        allocation. Raises :class:`BudgetExceeded` (scope ``"tenant"``) on a tenant
        breach — the same exception the scheduler/pipeline already handle.
        """
        if tenant_id is not None:
            await self._budget_repo.advisory_lock(_LOCK_KEY)
            await self._check_tenant_cap(tenant_id, video_seconds)
        return await self._budget.reserve(
            video_seconds,
            session_id=session_id,
            scene_id=scene_id,
            book_id=book_id,
            note=note,
        )

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        """Charge the actual seconds (passthrough to :class:`BudgetService`)."""
        await self._budget.commit(reservation, actual_seconds, note=note)

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        """Return an earmark (passthrough to :class:`BudgetService`)."""
        await self._budget.release(reservation, note=note)

    async def _check_tenant_cap(self, tenant_id: str, requested_s: float) -> None:
        used = await self.tenant_used_seconds(tenant_id)
        cap = self._policy.tenant_cap
        if cap.would_exceed_hard(used, requested_s):
            raise BudgetExceeded(
                "tenant", requested=requested_s, used=used, cap=cap.cap_s
            )

    # -- tenant usage -------------------------------------------------------- #

    async def tenant_book_ids(self, tenant_id: str) -> list[str]:
        """The book ids owned by a tenant (the tenant = book owner / user)."""
        books = await self._book_repo.list_for_user(tenant_id)
        return [b.id for b in books]

    async def tenant_used_seconds(self, tenant_id: str) -> float:
        """Committed + outstanding-reserved video-seconds across a tenant's books."""
        book_ids = await self.tenant_book_ids(tenant_id)
        return await self._budget_repo.used_seconds_for_books(book_ids)

    async def tenant_usage(self, tenant_id: str) -> TenantUsage:
        """A tenant's usage + its evaluated cap status."""
        used = await self.tenant_used_seconds(tenant_id)
        return TenantUsage(
            tenant_id=tenant_id, used_s=used, status=self._policy.tenant_cap.evaluate(used)
        )

    # -- per-scope usage snapshot -------------------------------------------- #

    async def used_by_scope(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> dict[BudgetScopeKind, float]:
        """Used video-seconds for every scope that applies to this snapshot."""
        used: dict[BudgetScopeKind, float] = {
            BudgetScopeKind.GLOBAL: await self._budget_repo.used_seconds(),
        }
        if tenant_id is not None:
            used[BudgetScopeKind.TENANT] = await self.tenant_used_seconds(tenant_id)
        if session_id is not None:
            used[BudgetScopeKind.SESSION] = await self._budget_repo.used_seconds(
                session_id=session_id
            )
        if scene_id is not None:
            used[BudgetScopeKind.SCENE] = await self._budget_repo.used_seconds(scene_id=scene_id)
        return used

    async def cap_statuses(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> list[CapStatus]:
        """Evaluated tiered-cap status for every applicable scope."""
        used = await self.used_by_scope(
            tenant_id=tenant_id, session_id=session_id, scene_id=scene_id
        )
        return self._policy.evaluate_all(used)

    async def worst_alert_level(
        self,
        *,
        tenant_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> AlertLevel:
        """The most severe alert level across applicable scopes."""
        statuses = await self.cap_statuses(
            tenant_id=tenant_id, session_id=session_id, scene_id=scene_id
        )
        return BudgetTierPolicy.worst_level(statuses)

    # -- governance decision ------------------------------------------------- #

    async def govern_session(
        self,
        *,
        trajectory: ReadingTrajectory,
        upcoming: list[ShotOption],
        tenant_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        horizon_s: float | None = None,
    ) -> GovernanceDecision:
        """The full governance verdict (caps + forecast + optimizer) for a session."""
        used = await self.used_by_scope(
            tenant_id=tenant_id, session_id=session_id, scene_id=scene_id
        )
        horizon = horizon_s if horizon_s is not None else self._forecast_horizon()
        return govern(
            self._policy,
            used_by_scope=used,
            trajectory=trajectory,
            upcoming=upcoming,
            horizon_s=horizon,
            min_quality=self._min_quality(),
        )

    def _forecast_horizon(self) -> float:
        if self._settings is not None:
            return float(self._settings.finops_forecast_horizon_s)
        return 600.0

    def _min_quality(self) -> float:
        if self._settings is not None:
            return float(self._settings.finops_optimizer_min_quality)
        return 0.0

    # -- cost ledger --------------------------------------------------------- #

    async def record_usage_cost(
        self,
        usage: Usage,
        cost_usd: Decimal,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        note: str | None = None,
    ) -> None:
        """Append a USD-valued cost row for one provider :class:`Usage` event.

        The agent is attributed deterministically (:func:`attribute_agent`) and the
        kind is mapped from the operation, so the cost ledger is self-describing
        for per-agent / per-kind rollups + reconciliation.
        """
        from app.db.models.finops import CostKind

        await self._cost_repo.append(
            kind=CostKind.from_operation(usage.operation),
            cost_usd=cost_usd,
            tenant_id=tenant_id,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
            shot_id=shot_id,
            agent=attribute_agent(usage).value,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            images=usage.images,
            audio_seconds=usage.audio_seconds,
            video_seconds=usage.video_seconds,
            note=note,
        )

    async def cost_summary(
        self,
        *,
        tenant_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
    ) -> CostSummary:
        """A scoped USD + per-agent + per-kind cost summary (for the API/HUD)."""
        total = await self._cost_repo.total_usd(
            tenant_id=tenant_id, book_id=book_id, session_id=session_id
        )
        video_s = await self._cost_repo.video_seconds_total(
            tenant_id=tenant_id, book_id=book_id, session_id=session_id
        )
        by_agent = await self._cost_repo.by_agent_micros(
            tenant_id=tenant_id, book_id=book_id, session_id=session_id
        )
        by_kind = await self._cost_repo.by_kind_micros(
            tenant_id=tenant_id, book_id=book_id, session_id=session_id
        )
        label = _scope_label(tenant_id=tenant_id, book_id=book_id, session_id=session_id)
        return CostSummary(
            scope_label=label,
            cost_usd=total,
            video_seconds=video_s,
            by_agent_usd={a: micros_to_usd(m) for a, m in by_agent.items()},
            by_kind_usd={k: micros_to_usd(m) for k, m in by_kind.items()},
        )

    # -- reconciliation ------------------------------------------------------ #

    async def reconcile_video_seconds(
        self,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
        tolerance_s: float = 0.1,
    ) -> Reconciliation:
        """Reconcile the cost ledger's video-seconds against the budget ledger.

        The budget ledger's *committed* seconds are authoritative (the scarce
        currency actually charged); the cost ledger should record the same total.
        Drift beyond ``tolerance_s`` flags a mis-recorded render.
        """
        committed = await self._budget_repo.committed_seconds(
            book_id=book_id, session_id=session_id, scene_id=scene_id
        )
        recorded = await self._cost_repo.video_seconds_total(
            book_id=book_id, session_id=session_id, scene_id=scene_id
        )
        return reconcile(
            scope_label=_scope_label(book_id=book_id, session_id=session_id, scene_id=scene_id),
            budget_committed_s=committed,
            cost_recorded_s=recorded,
            tolerance_s=tolerance_s,
        )


def _scope_label(
    *,
    tenant_id: str | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
    scene_id: str | None = None,
) -> str:
    """A compact, deterministic label for a scope (``global`` when all None)."""
    parts: list[str] = []
    if tenant_id is not None:
        parts.append(f"tenant={tenant_id}")
    if book_id is not None:
        parts.append(f"book={book_id}")
    if session_id is not None:
        parts.append(f"session={session_id}")
    if scene_id is not None:
        parts.append(f"scene={scene_id}")
    return ",".join(parts) if parts else "global"


__all__ = ["FinOpsService", "TenantUsage"]
