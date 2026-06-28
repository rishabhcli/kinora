"""Billing & payments API routes (mounted additively under ``/api``).

The authenticated routes (plans, subscription lifecycle, usage, invoices,
entitlements) resolve the caller's billing customer from their user id. The
webhook route is unauthenticated by design — it is protected by the provider's
**signature** (HMAC-SHA256 + timestamp), verified before anything is applied, and
is idempotent on the event id.

Billing-domain errors (:class:`app.billing.errors.BillingError`) are translated
to the gateway's :class:`app.api.errors.APIError` so they render as the standard
``{"error": {...}}`` envelope with the right status (e.g. 402 for an entitlement
gate, 404 for a missing entity). No real Stripe/network/payment call happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, Depends, Request

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.billing.enums import BillingInterval, UsageMeter
from app.billing.errors import BillingError, WebhookVerificationError
from app.billing.schemas import (
    CancelSubscriptionRequest,
    ChangePlanRequest,
    CreateSubscriptionRequest,
    EntitlementsView,
    PlansResponse,
    RecordUsageRequest,
    SubscriptionView,
    UsageView,
    WebhookResponse,
)
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.billing.service import BillingService
    from app.billing.webhooks import WebhookHandler

logger = get_logger("app.api.billing")

router = APIRouter(tags=["billing"])


def _service(container: ContainerDep) -> BillingService:
    return cast("BillingService", container.billing_service)


def _interval(value: str) -> BillingInterval:
    try:
        return BillingInterval(value)
    except ValueError as exc:
        raise APIError("invalid_interval", f"unknown interval {value!r}", status=422) from exc


def _meter(value: str) -> UsageMeter:
    try:
        return UsageMeter(value)
    except ValueError as exc:
        raise APIError("invalid_meter", f"unknown meter {value!r}", status=422) from exc


def _as_api_error(exc: BillingError) -> APIError:
    detail: dict[str, object] | None = None
    if exc.code == "entitlement_denied":
        detail = {
            "feature": getattr(exc, "feature", None),
            "required_tier": getattr(exc, "required_tier", None),
        }
    return APIError(exc.code, exc.message, status=exc.http_status, detail=detail)


async def _customer_id(container: ContainerDep, user: CurrentUser) -> str:
    return await _service(container).ensure_customer(user_id=user.id, email=user.email)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


@router.get("/billing/plans", response_model=PlansResponse)
async def list_plans(container: ContainerDep) -> PlansResponse:
    """The active plan catalog for the pricing page (public-ish; auth not required)."""
    plans = await _service(container).list_plans()
    return PlansResponse(plans=plans)


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #


@router.post("/billing/subscription", response_model=SubscriptionView)
async def create_subscription(
    body: CreateSubscriptionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SubscriptionView:
    """Start a subscription for the authenticated user (begins a trial if offered)."""
    customer_id = await _customer_id(container, user)
    try:
        sub = await _service(container).create_subscription(
            customer_id=customer_id,
            plan_code=body.plan_code,
            price_id=body.price_id,
            interval=_interval(body.interval),
            coupon_code=body.coupon_code,
        )
    except BillingError as exc:
        raise _as_api_error(exc) from exc
    return SubscriptionView(**sub)


@router.post("/billing/subscription/{subscription_id}/change", response_model=SubscriptionView)
async def change_plan(
    subscription_id: str,
    body: ChangePlanRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SubscriptionView:
    """Upgrade/downgrade a subscription (computes proration)."""
    await _assert_owns(container, user, subscription_id)
    try:
        sub = await _service(container).change_plan(
            subscription_id=subscription_id,
            new_plan_code=body.new_plan_code,
            new_price_id=body.new_price_id,
            interval=_interval(body.interval),
        )
    except BillingError as exc:
        raise _as_api_error(exc) from exc
    return SubscriptionView(**sub)


@router.post("/billing/subscription/{subscription_id}/cancel", response_model=SubscriptionView)
async def cancel_subscription(
    subscription_id: str,
    body: CancelSubscriptionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SubscriptionView:
    """Cancel a subscription (at period end by default, or immediately)."""
    await _assert_owns(container, user, subscription_id)
    try:
        sub = await _service(container).cancel_subscription(
            subscription_id=subscription_id, at_period_end=body.at_period_end
        )
    except BillingError as exc:
        raise _as_api_error(exc) from exc
    return SubscriptionView(**sub)


# --------------------------------------------------------------------------- #
# Entitlements + usage
# --------------------------------------------------------------------------- #


@router.get("/billing/entitlements", response_model=EntitlementsView)
async def my_entitlements(container: ContainerDep, user: CurrentUser) -> EntitlementsView:
    """The authenticated user's feature gates + per-meter allowances."""
    customer_id = await _customer_id(container, user)
    ent = await _service(container).entitlements_for(customer_id)
    return EntitlementsView(
        tier=ent.tier.value,
        plan_code=ent.plan_code,
        active=ent.active,
        features={k: v.limit for k, v in ent.features.items()},
        allowances={m.value: a.included_units for m, a in ent.allowances.items()},
    )


@router.post("/billing/usage", status_code=202)
async def record_usage(
    body: RecordUsageRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, object]:
    """Report a metered usage event for the user's active subscription."""
    service = _service(container)
    customer_id = await _customer_id(container, user)
    sub = await _active_subscription_id(container, customer_id)
    recorded = await service.record_usage(
        meter=_meter(body.meter),
        quantity=body.quantity,
        subscription_id=sub,
        customer_id=customer_id,
        idempotency_key=body.idempotency_key,
        book_id=body.book_id,
        session_id=body.session_id,
    )
    return {"recorded": recorded}


@router.get("/billing/subscription/{subscription_id}/usage", response_model=UsageView)
async def subscription_usage(
    subscription_id: str, container: ContainerDep, user: CurrentUser
) -> UsageView:
    """Aggregated current-period usage by meter for a subscription."""
    await _assert_owns(container, user, subscription_id)
    try:
        summary = await _service(container).usage_summary(subscription_id)
    except BillingError as exc:
        raise _as_api_error(exc) from exc
    return UsageView(
        period_start=summary.period_start.isoformat() if summary.period_start else None,
        period_end=summary.period_end.isoformat() if summary.period_end else None,
        by_meter={m.value: q.quantity for m, q in summary.by_meter.items()},
    )


# --------------------------------------------------------------------------- #
# Inbound webhook (signature-verified, idempotent, unauthenticated)
# --------------------------------------------------------------------------- #


@router.post("/billing/webhook", response_model=WebhookResponse)
async def billing_webhook(request: Request, container: ContainerDep) -> WebhookResponse:
    """Apply a signed provider webhook (HMAC-verified + idempotent).

    The body is verified against the provider's webhook secret before anything is
    applied; a bad/stale signature is a 400. There is no auth header — the
    signature *is* the authentication.
    """
    payload = await request.body()
    signature = request.headers.get("x-billing-signature") or request.headers.get(
        "stripe-signature", ""
    )
    handler = cast("WebhookHandler", container.build_billing_webhook_handler())
    try:
        result = await handler.handle(payload=payload, signature_header=signature)
    except WebhookVerificationError as exc:
        raise APIError("webhook_invalid_signature", str(exc), status=400) from exc
    return WebhookResponse(
        event_id=result.event_id, event_type=result.event_type, status=result.status
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _assert_owns(container: ContainerDep, user: CurrentUser, subscription_id: str) -> None:
    """Fail-closed ownership check: the subscription's customer maps to this user."""
    from app.billing.repositories import CustomerRepo, SubscriptionRepo

    async with container.session_factory() as db:
        sub = await SubscriptionRepo(db).get(subscription_id)
        if sub is None:
            raise APIError("subscription_not_found", "no such subscription", status=404)
        customer = await CustomerRepo(db).get(sub.customer_id)
    if customer is None or customer.user_id != user.id:
        raise APIError("subscription_not_found", "no such subscription", status=404)


async def _active_subscription_id(container: ContainerDep, customer_id: str) -> str | None:
    from app.billing.repositories import SubscriptionRepo

    async with container.session_factory() as db:
        sub = await SubscriptionRepo(db).active_for_customer(customer_id)
    return sub.id if sub is not None else None


__all__ = ["router"]
