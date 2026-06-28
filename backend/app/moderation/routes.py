"""API routes for the moderation subsystem (§9/§10), mounted under ``/api``.

A self-contained admin/operations surface over the moderation service:

* ``POST /moderation/screen/text`` — screen an arbitrary text payload (ingest or
  comment surface) — the operational hook + a live demo of the gate.
* ``GET  /moderation/queue`` — the human-review queue (optionally by state).
* ``GET  /moderation/queue/{id}`` — one review item.
* ``POST /moderation/queue/{id}/{action}`` — drive the review state machine
  (claim / approve / reject / takedown / escalate / appeal / grant / deny).
* ``GET  /moderation/offenders`` — repeat-offenders + their enforcement tier.
* ``GET  /moderation/actors/{actor_id}`` — one actor's standing.
* ``POST /moderation/actors/{actor_id}/reinstate`` — clear a suspension/ban.
* ``GET  /moderation/audit`` — the tamper-evident audit chain + its verification.
* ``GET  /moderation/policy`` / ``PUT /moderation/policy`` — read/write the
  configurable per-tenant policy.
* ``GET  /moderation/stats`` — the compact moderation dashboard payload.

All routes require an authenticated user (the existing :data:`CurrentUser` dep);
write routes additionally pass through the shared write rate-limiter. The tenant
is taken from a query/body field (default ``"default"``) so a single deployment
can operate several policy tenants; a production multi-tenant wiring would derive
it from the authenticated principal instead.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.moderation.contracts import (
    Decision,
    ModerationContext,
    ReviewItemView,
    ReviewState,
    Surface,
)
from app.moderation.review import ReviewTransitionError
from app.moderation.tenant_policy import TenantPolicy

logger = get_logger("app.moderation.routes")

router = APIRouter(prefix="/moderation", tags=["moderation"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ScreenTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)
    tenant_id: str = "default"
    surface: Surface = Surface.COMMENT
    book_id: str | None = None
    shot_id: str | None = None


class ScreenResponse(BaseModel):
    decision: Decision
    disposition: str
    severity: int
    categories: list[str]
    reason: str
    degraded: bool
    event_id: str | None
    review_item_id: str | None
    actor_blocked: bool


class ReviewActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class ActorStatusResponse(BaseModel):
    actor_id: str
    tier: str
    window_count: int
    total_count: int
    generation_blocked: bool
    throttled: bool
    suspended_until: str | None


class AuditEntryResponse(BaseModel):
    seq: int
    action: str
    actor_id: str
    target_id: str | None
    entry_hash: str
    prev_hash: str | None


class AuditChainResponse(BaseModel):
    tenant_id: str
    intact: bool
    broken_at_seq: int | None
    entries: list[AuditEntryResponse]


class PolicyResponse(BaseModel):
    tenant_id: str
    version: str
    strictness: float
    fail_closed_on_degraded: bool
    serve_flagged: bool
    auto_takedown_at: int
    overrides: dict[str, Any]


# --------------------------------------------------------------------------- #
# Screening
# --------------------------------------------------------------------------- #


@router.post("/screen/text", response_model=ScreenResponse)
async def screen_text(
    body: ScreenTextRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ScreenResponse:
    """Screen a text payload through the gate (ingest/comment surface)."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        ctx = ModerationContext(
            tenant_id=body.tenant_id,
            user_id=user.id,
            book_id=body.book_id,
            shot_id=body.shot_id,
        )
        if body.surface is Surface.INGEST_TEXT:
            result = await svc.screen_book_text(body.text, context=ctx)
        else:
            result = await svc.screen_comment(body.text, context=ctx)
    return _screen_response(result)


# --------------------------------------------------------------------------- #
# Review queue + state machine
# --------------------------------------------------------------------------- #


@router.get("/queue", response_model=list[ReviewItemView])
async def list_queue(
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
    state: ReviewState | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[ReviewItemView]:
    """The human-review queue, worst-severity-first (optionally filtered by state)."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        return await svc.queue(tenant_id, state=state, limit=limit)


@router.get("/queue/{item_id}", response_model=ReviewItemView)
async def get_review_item(
    item_id: str, container: ContainerDep, user: CurrentUser
) -> ReviewItemView:
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        item = await svc.review.view(item_id)
    if item is None:
        raise APIError("review_item_not_found", "no such review item", status=404)
    return item


_ACTIONS = {"claim", "approve", "reject", "takedown", "escalate", "appeal", "grant", "deny"}


@router.post("/queue/{item_id}/{action}", response_model=ReviewItemView)
async def review_action(
    item_id: str,
    action: str,
    body: ReviewActionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ReviewItemView:
    """Drive the review state machine; 409 on an illegal transition."""
    if action not in _ACTIONS:
        raise APIError("bad_action", f"unknown review action {action!r}", status=400)
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        wf = svc.review
        try:
            if action == "claim":
                await wf.claim(item_id, reviewer_id=user.id)
            elif action == "approve":
                await wf.approve(item_id, reviewer_id=user.id, note=body.note)
            elif action == "reject":
                await wf.reject(item_id, reviewer_id=user.id, note=body.note)
            elif action == "takedown":
                await wf.takedown(item_id, reviewer_id=user.id, note=body.note)
            elif action == "escalate":
                await wf.escalate(item_id, actor_id=user.id, note=body.note)
            elif action == "appeal":
                await wf.appeal(item_id, appellant_id=user.id, note=body.note)
            elif action == "grant":
                await wf.grant_appeal(item_id, reviewer_id=user.id, note=body.note)
            else:  # deny
                await wf.deny_appeal(item_id, reviewer_id=user.id, note=body.note)
        except ReviewTransitionError as exc:
            raise APIError("illegal_transition", str(exc), status=409) from exc
        item = await wf.view(item_id)
    if item is None:
        raise APIError("review_item_not_found", "no such review item", status=404)
    logger.info("moderation.review_action", item_id=item_id, action=action, actor=user.id)
    return item


# --------------------------------------------------------------------------- #
# Escalation / offenders
# --------------------------------------------------------------------------- #


@router.get("/offenders", response_model=list[ActorStatusResponse])
async def list_offenders(
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
    min_tier: int = Query(1, ge=0, le=4),
) -> list[ActorStatusResponse]:
    """Repeat offenders at/above ``min_tier``, worst-first."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        outcomes = await svc.escalation.offenders(tenant_id, min_tier=min_tier)
    return [_actor_status(o) for o in outcomes]


@router.get("/actors/{actor_id}", response_model=ActorStatusResponse)
async def actor_status(
    actor_id: str,
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
) -> ActorStatusResponse:
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        outcome = await svc.actor_status(tenant_id=tenant_id, actor_id=actor_id)
    return _actor_status(outcome)


@router.post("/actors/{actor_id}/reinstate", response_model=ActorStatusResponse)
async def reinstate_actor(
    actor_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    tenant_id: str = Query("default"),
) -> ActorStatusResponse:
    """Manually clear an actor's suspension/ban (an amnesty or appeal grant)."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        outcome = await svc.escalation.reinstate(
            tenant_id=tenant_id, actor_id=actor_id, reviewer_id=user.id
        )
    return _actor_status(outcome)


# --------------------------------------------------------------------------- #
# Audit + policy + stats
# --------------------------------------------------------------------------- #


@router.get("/audit", response_model=AuditChainResponse)
async def audit_chain(
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
    limit: int = Query(200, ge=1, le=2000),
) -> AuditChainResponse:
    """The tamper-evident moderation audit chain + its verification result."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        chain = await svc.audit_chain(tenant_id, limit=limit)
    return AuditChainResponse(
        tenant_id=chain.tenant_id,
        intact=chain.intact,
        broken_at_seq=chain.broken_at_seq,
        entries=[
            AuditEntryResponse(
                seq=e.seq,
                action=e.action,
                actor_id=e.actor_id,
                target_id=e.target_id,
                entry_hash=e.entry_hash,
                prev_hash=e.prev_hash,
            )
            for e in chain.entries
        ],
    )


@router.get("/policy", response_model=PolicyResponse)
async def get_policy(
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
) -> PolicyResponse:
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        policy = await svc.resolve_policy(tenant_id)
    return _policy_response(policy)


@router.put("/policy", response_model=PolicyResponse)
async def set_policy(
    body: TenantPolicy,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> PolicyResponse:
    """Persist a configurable per-tenant policy (zero-tolerance floor is enforced)."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        policy = await svc.set_policy(body)
    logger.info("moderation.policy_set", tenant_id=body.tenant_id, version=body.version)
    return _policy_response(policy)


@router.get("/stats")
async def stats(
    container: ContainerDep,
    user: CurrentUser,
    tenant_id: str = Query("default"),
) -> dict[str, Any]:
    """The compact moderation dashboard payload for a tenant."""
    async with container.session_factory() as session:
        svc = container.build_moderation(session)
        return await svc.event_stats(tenant_id)


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


def _screen_response(result: Any) -> ScreenResponse:
    return ScreenResponse(
        decision=result.decision,
        disposition=result.verdict.decision.value,
        severity=int(result.verdict.severity),
        categories=[c.value for c in result.verdict.categories],
        reason=result.verdict.reason,
        degraded=result.verdict.degraded,
        event_id=result.event_id,
        review_item_id=result.review_item_id,
        actor_blocked=result.actor_blocked,
    )


def _actor_status(outcome: Any) -> ActorStatusResponse:
    return ActorStatusResponse(
        actor_id=outcome.actor_id,
        tier=outcome.tier.label,
        window_count=outcome.window_count,
        total_count=outcome.total_count,
        generation_blocked=outcome.generation_blocked,
        throttled=outcome.throttled,
        suspended_until=outcome.suspended_until.isoformat()
        if outcome.suspended_until
        else None,
    )


def _policy_response(policy: TenantPolicy) -> PolicyResponse:
    return PolicyResponse(
        tenant_id=policy.tenant_id,
        version=policy.version,
        strictness=policy.strictness,
        fail_closed_on_degraded=policy.fail_closed_on_degraded,
        serve_flagged=policy.serve_flagged,
        auto_takedown_at=int(policy.auto_takedown_at),
        overrides={k.value: v.model_dump(mode="json") for k, v in policy.overrides.items()},
    )


__all__ = ["router"]
