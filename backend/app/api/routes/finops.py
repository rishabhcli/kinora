"""FinOps API — budget & cost governance surface (kinora.md §11.1, §12.5).

Read-only views over the FinOps layer (:mod:`app.finops`), all behind auth (spend
is sensitive). The authenticated user **is** the tenant, so per-tenant numbers are
scoped to their own books — no cross-tenant leakage.

* ``GET /finops/budget`` — the tiered-cap status for global + this tenant (+ an
  optional ``session_id`` / ``scene_id`` scope), the worst alert level, and the
  binding scope.
* ``GET /finops/cost`` — the USD cost summary for the tenant (or a narrower scope),
  with per-agent and per-kind breakdowns from the cost ledger.
* ``GET /finops/reconcile`` — reconcile the cost ledger's video-seconds against the
  authoritative budget ledger for a scope (surfaces mis-recorded renders).
* ``POST /finops/forecast`` — a governance preview: given a reading trajectory and
  upcoming shot costs, return the forecast burn-down + the quality↔budget optimizer
  plan + the recommendation (``promote``/``optimize``/``halt``). Pure (no spend).
* ``POST /finops/simulate`` — run the no-infra synthetic-session harness against the
  active caps and prove the system stays inside budget (zero credits).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser
from app.finops.forecast import ReadingTrajectory
from app.finops.optimizer import ShotOption
from app.finops.simulation import SyntheticReader, run_suite, simulate_reader
from app.finops.tiers import BudgetTierPolicy

router = APIRouter(prefix="/finops", tags=["finops"])


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class TrajectoryIn(BaseModel):
    """A reading trajectory for a forecast/governance preview."""

    velocity_wps: float = Field(ge=0.0)
    words_remaining: int = Field(ge=0)
    shot_seconds_per_word: float = Field(default=0.02, ge=0.0)
    promotion_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class ShotOptionIn(BaseModel):
    """One upcoming shot's full-video cost + importance for the optimizer."""

    shot_id: str
    video_seconds: float = Field(ge=0.0)
    importance: float = Field(default=1.0, ge=0.0)


class ForecastIn(BaseModel):
    """A forecast/governance preview request."""

    trajectory: TrajectoryIn
    upcoming: list[ShotOptionIn] = Field(default_factory=list)
    session_id: str | None = None
    scene_id: str | None = None
    horizon_s: float | None = Field(default=None, ge=0.0)


class SimReaderIn(BaseModel):
    """One synthetic reader for the simulation harness."""

    label: str
    velocity_wps: float = Field(gt=0.0)
    total_words: int = Field(gt=0)
    promotion_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    shot_video_seconds: float = Field(default=5.0, ge=0.0)
    importance: float = Field(default=1.0, ge=0.0)


class SimulateIn(BaseModel):
    """A simulation request: a custom reader suite, or empty for the defaults."""

    readers: list[SimReaderIn] = Field(default_factory=list)
    tick_s: float = Field(default=5.0, gt=0.0)
    max_ticks: int = Field(default=400, gt=0, le=5000)
    horizon_s: float = Field(default=60.0, ge=0.0)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/budget")
async def get_budget(
    container: ContainerDep,
    user: CurrentUser,
    session_id: str | None = Query(default=None),
    scene_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Tiered-cap status across global + this tenant (+ optional session/scene)."""
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        statuses = await finops.cap_statuses(
            tenant_id=user.id, session_id=session_id, scene_id=scene_id
        )
        worst = BudgetTierPolicy.worst_level(statuses)
        binding = BudgetTierPolicy.binding_scope(statuses)
    return {
        "tenant_id": user.id,
        "worst_level": worst.label,
        "binding_scope": binding.scope.value if binding else None,
        "statuses": [s.as_dict() for s in statuses],
    }


@router.get("/cost")
async def get_cost(
    container: ContainerDep,
    user: CurrentUser,
    book_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """USD cost summary for the tenant (or a narrower book/session scope)."""
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        summary = await finops.cost_summary(
            tenant_id=user.id, book_id=book_id, session_id=session_id
        )
    return summary.as_dict()


@router.get("/reconcile")
async def get_reconcile(
    container: ContainerDep,
    user: CurrentUser,
    book_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    scene_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Reconcile cost-ledger vs. budget-ledger video-seconds for a scope."""
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        recon = await finops.reconcile_video_seconds(
            book_id=book_id, session_id=session_id, scene_id=scene_id
        )
    return recon.as_dict()


@router.post("/forecast")
async def post_forecast(
    container: ContainerDep,
    user: CurrentUser,
    body: ForecastIn,
) -> dict[str, Any]:
    """Governance preview: forecast burn-down + optimizer plan + recommendation."""
    trajectory = ReadingTrajectory(
        velocity_wps=body.trajectory.velocity_wps,
        words_remaining=body.trajectory.words_remaining,
        shot_seconds_per_word=body.trajectory.shot_seconds_per_word,
        promotion_rate=body.trajectory.promotion_rate,
    )
    upcoming = [
        ShotOption(shot_id=s.shot_id, video_seconds=s.video_seconds, importance=s.importance)
        for s in body.upcoming
    ]
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        decision = await finops.govern_session(
            trajectory=trajectory,
            upcoming=upcoming,
            tenant_id=user.id,
            session_id=body.session_id,
            scene_id=body.scene_id,
            horizon_s=body.horizon_s,
        )
    return decision.as_dict()


@router.post("/simulate")
async def post_simulate(
    container: ContainerDep,
    user: CurrentUser,
    body: SimulateIn,
) -> dict[str, Any]:
    """Run the no-infra budget simulation against the active caps (zero credits)."""
    policy = container.finops_policy
    if body.readers:
        results = [
            simulate_reader(
                SyntheticReader(
                    label=r.label,
                    velocity_wps=r.velocity_wps,
                    total_words=r.total_words,
                    promotion_rate=r.promotion_rate,
                    shot_video_seconds=r.shot_video_seconds,
                    importance=r.importance,
                ),
                policy,
                tick_s=body.tick_s,
                max_ticks=body.max_ticks,
                horizon_s=body.horizon_s,
            ).as_dict()
            for r in body.readers
        ]
        return {"any_cap_breached": any(r["cap_breached"] for r in results), "results": results}
    report = run_suite(
        policy, tick_s=body.tick_s, max_ticks=body.max_ticks, horizon_s=body.horizon_s
    )
    return report.as_dict()


__all__ = ["router"]
