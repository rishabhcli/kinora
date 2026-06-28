"""``BillingService`` — the orchestration facade for the billing domain.

This ties the pure math (catalog / proration / coupons / tax / invoicing /
dunning) to persistence (the repos) and to the payment-provider abstraction (the
fake transport by default). It is the surface the API routes and the composition
root call. Every mutation writes an append-only audit entry, mirroring the budget
ledger's auditability (§11).

The service is constructed with a unit-of-work ``session_factory`` (so each
operation runs in its own committing transaction) and a
:class:`app.billing.provider.base.PaymentProvider` (the fake by default — **no
real Stripe/network/payment call is ever made**). ``KINORA_LIVE_VIDEO`` is
irrelevant here and untouched; billing spends no credits.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.dunning import DunningSchedule, DunningState
from app.billing.entitlements import Entitlements, project_entitlements
from app.billing.enums import (
    AuditEvent,
    BillingInterval,
    InvoiceStatus,
    MeteredAggregation,
    PaymentStatus,
    PlanTier,
    SubscriptionStatus,
    TaxBehavior,
    UsageMeter,
)
from app.billing.errors import (
    InvalidStateError,
    PlanNotFoundError,
    PriceNotFoundError,
    SubscriptionNotFoundError,
)
from app.billing.hydration import coupon_from_row, plan_from_row
from app.billing.invoicing import DraftInvoice, InvoiceNumberFormatter
from app.billing.metering import UsageQuantity, UsageSummary
from app.billing.models import (
    BillingAuditLog,
    BillingCustomer,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPaymentAttempt,
    BillingSubscription,
)
from app.billing.money import Money
from app.billing.period_billing import PeriodChargeContext, build_period_invoice
from app.billing.proration import ProrationResult, compute_plan_change_proration
from app.billing.provider.base import PaymentProvider
from app.billing.provider.fake import FakePaymentProvider
from app.billing.repositories import (
    AuditRepo,
    CouponRepo,
    CustomerRepo,
    InvoiceRepo,
    PaymentAttemptRepo,
    PlanRepo,
    SubscriptionRepo,
    UsageRepo,
)
from app.billing.seed import seed_default_catalog
from app.billing.tax import TaxRate, TaxRateResolver

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
# Coerce a SubscriptionStatus or its str value to a SubscriptionStatus.


@dataclass(frozen=True, slots=True)
class BillingConfig:
    """Tunables the service reads (defaults are safe + offline)."""

    default_currency: str = "USD"
    invoice_prefix: str = "KIN"
    dunning_retry_days: tuple[int, ...] = (1, 3, 5, 7)
    tax_behavior: TaxBehavior = TaxBehavior.EXCLUSIVE
    auto_charge_on_finalize: bool = True


@dataclass
class BillingService:
    """Orchestrates the billing lifecycle over the repos + a payment provider."""

    session_factory: SessionFactory
    provider: PaymentProvider = field(default_factory=FakePaymentProvider)
    config: BillingConfig = field(default_factory=BillingConfig)
    tax_resolver: TaxRateResolver = field(default_factory=TaxRateResolver)

    # -- catalog ------------------------------------------------------------- #

    async def seed_catalog(self) -> int:
        """Persist the default plan catalog (idempotent); return created count."""
        async with self.session_factory() as db:
            return await seed_default_catalog(PlanRepo(db))

    async def list_plans(self) -> list[dict[str, object]]:
        """The active plans projected for the pricing page."""
        async with self.session_factory() as db:
            repo = PlanRepo(db)
            plans = await repo.list_active()
            out: list[dict[str, object]] = []
            for plan in plans:
                prices = await repo.prices_for_plan(plan.id)
                out.append(
                    {
                        "id": plan.id,
                        "code": plan.code,
                        "name": plan.name,
                        "tier": plan.tier.value,
                        "trial_days": plan.trial_days,
                        "description": plan.description,
                        "prices": [
                            {
                                "id": p.id,
                                "type": p.type.value,
                                "interval": p.interval.value,
                                "currency": p.currency,
                                "flat_amount_minor": p.flat_amount_minor,
                                "unit_amount_minor": p.unit_amount_minor,
                                "included_units": p.included_units,
                                "meter": p.meter.value if p.meter else None,
                            }
                            for p in prices
                        ],
                    }
                )
            return out

    # -- customers ----------------------------------------------------------- #

    async def ensure_customer(
        self, *, user_id: str, email: str | None = None, currency: str | None = None
    ) -> str:
        """Get-or-create the billing customer for a user; return its id."""
        async with self.session_factory() as db:
            repo = CustomerRepo(db)
            existing = await repo.get_by_user(user_id)
            if existing is not None:
                return existing.id
            provider_customer = self.provider.create_customer(
                email=email, metadata={"user_id": user_id}
            )
            customer = await repo.create(
                BillingCustomer(
                    user_id=user_id,
                    email=email,
                    provider=self.provider.config.name,
                    provider_customer_id=provider_customer.id,
                    default_currency=(currency or self.config.default_currency),
                )
            )
            await self._audit(
                db,
                AuditEvent.CUSTOMER_CREATED,
                actor=user_id,
                customer_id=customer.id,
            )
            return customer.id

    # -- subscriptions ------------------------------------------------------- #

    async def create_subscription(
        self,
        *,
        customer_id: str,
        plan_code: str,
        price_id: str | None = None,
        interval: BillingInterval = BillingInterval.MONTH,
        coupon_code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Start a subscription, beginning a trial if the plan offers one."""
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            plan_repo = PlanRepo(db)
            plan_row = await plan_repo.get_by_code(plan_code)
            if plan_row is None:
                raise PlanNotFoundError(f"no plan {plan_code!r}")
            prices = await plan_repo.prices_for_plan(plan_row.id)
            chosen = self._resolve_price(prices, price_id, interval)
            currency = chosen.currency

            trialing = plan_row.trial_days > 0
            if trialing:
                status = SubscriptionStatus.TRIALING
                period_start = now
                period_end = now + timedelta(days=plan_row.trial_days)
                trial_start: datetime | None = now
                trial_end: datetime | None = period_end
            else:
                status = SubscriptionStatus.ACTIVE
                period_start = now
                period_end = _advance_period(now, chosen.interval)
                trial_start = trial_end = None

            sub = await SubscriptionRepo(db).create(
                BillingSubscription(
                    customer_id=customer_id,
                    plan_id=plan_row.id,
                    price_id=chosen.id,
                    status=status,
                    currency=currency,
                    current_period_start=period_start,
                    current_period_end=period_end,
                    trial_start=trial_start,
                    trial_end=trial_end,
                    coupon_code=coupon_code,
                    period_index=0,
                )
            )
            await self._audit(
                db,
                AuditEvent.SUBSCRIPTION_CREATED,
                customer_id=customer_id,
                subscription_id=sub.id,
                plan=plan_code,
                price_id=chosen.id,
            )
            if trialing:
                await self._audit(
                    db, AuditEvent.TRIAL_STARTED, customer_id=customer_id, subscription_id=sub.id
                )
            return self._sub_view(sub)

    async def change_plan(
        self,
        *,
        subscription_id: str,
        new_plan_code: str,
        new_price_id: str | None = None,
        interval: BillingInterval = BillingInterval.MONTH,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Upgrade/downgrade a subscription mid-period, computing proration.

        The unused remainder of the old price is credited and the new price is
        charged for the remaining period; the net is recorded as a proration
        invoice. The subscription moves to the new plan immediately.
        """
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            sub_repo = SubscriptionRepo(db)
            sub = await sub_repo.get(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"no subscription {subscription_id!r}")
            if sub.status not in (
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.TRIALING,
                SubscriptionStatus.PAST_DUE,
            ):
                raise InvalidStateError(f"cannot change plan from status {sub.status.value}")

            plan_repo = PlanRepo(db)
            old_price = await plan_repo.get_price(sub.price_id)
            new_plan = await plan_repo.get_by_code(new_plan_code)
            if new_plan is None:
                raise PlanNotFoundError(f"no plan {new_plan_code!r}")
            new_prices = await plan_repo.prices_for_plan(new_plan.id)
            new_price = self._resolve_price(new_prices, new_price_id, interval)

            old_flat = (
                Money(old_price.flat_amount_minor or 0, sub.currency)
                if old_price
                else Money.zero(sub.currency)
            )
            new_flat = Money(new_price.flat_amount_minor or 0, new_price.currency)

            proration = None
            if (
                sub.current_period_start is not None
                and sub.current_period_end is not None
                and sub.current_period_end > now > sub.current_period_start
                and old_flat.currency == new_flat.currency
            ):
                proration = compute_plan_change_proration(
                    old_amount=old_flat,
                    new_amount=new_flat,
                    period_start=sub.current_period_start,
                    period_end=sub.current_period_end,
                    at=now,
                )

            old_plan_row = await plan_repo.get(sub.plan_id) if sub.plan_id else None
            old_plan_code = old_plan_row.code if old_plan_row is not None else None
            sub.plan_id = new_plan.id
            sub.price_id = new_price.id
            sub.currency = new_price.currency
            if sub.status is SubscriptionStatus.TRIALING:
                # Changing plan during a trial keeps the trial window.
                pass
            await db.flush()

            invoice_view: dict[str, object] | None = None
            if proration is not None and not proration.net.is_zero:
                # Only the proration lines belong to this immediate invoice (the
                # flat fee is billed at the next renewal), so build a focused draft.
                draft = self._proration_only_invoice(proration, new_price.currency)
                invoice_view = await self._persist_invoice(
                    db,
                    sub,
                    draft,
                    period_start=now,
                    period_end=sub.current_period_end,
                    now=now,
                    finalize=True,
                )

            await self._audit(
                db,
                AuditEvent.SUBSCRIPTION_UPDATED,
                customer_id=sub.customer_id,
                subscription_id=sub.id,
                from_plan=old_plan_code,
                to_plan=new_plan_code,
            )
            view = self._sub_view(sub)
            if invoice_view is not None:
                view["proration_invoice"] = invoice_view
            return view

    async def cancel_subscription(
        self, *, subscription_id: str, at_period_end: bool = True, now: datetime | None = None
    ) -> dict[str, object]:
        """Cancel a subscription, immediately or at period end."""
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            sub = await SubscriptionRepo(db).get(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"no subscription {subscription_id!r}")
            if sub.status is SubscriptionStatus.CANCELED:
                raise InvalidStateError("subscription already canceled")
            if at_period_end:
                sub.cancel_at_period_end = True
            else:
                sub.status = SubscriptionStatus.CANCELED
                sub.canceled_at = now
            await db.flush()
            await self._audit(
                db,
                AuditEvent.SUBSCRIPTION_CANCELED,
                customer_id=sub.customer_id,
                subscription_id=sub.id,
                at_period_end=at_period_end,
            )
            return self._sub_view(sub)

    async def activate_trial(
        self, *, subscription_id: str, now: datetime | None = None
    ) -> dict[str, object]:
        """End a trial and move the subscription to active (first paid period)."""
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            sub = await SubscriptionRepo(db).get(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"no subscription {subscription_id!r}")
            if sub.status is not SubscriptionStatus.TRIALING:
                raise InvalidStateError("subscription is not trialing")
            price = await PlanRepo(db).get_price(sub.price_id)
            interval = price.interval if price else BillingInterval.MONTH
            sub.status = SubscriptionStatus.ACTIVE
            sub.current_period_start = now
            sub.current_period_end = _advance_period(now, interval)
            await db.flush()
            await self._audit(
                db, AuditEvent.TRIAL_ENDED, customer_id=sub.customer_id, subscription_id=sub.id
            )
            return self._sub_view(sub)

    # -- entitlements -------------------------------------------------------- #

    async def entitlements_for(self, customer_id: str) -> Entitlements:
        """Project the customer's active subscription into an entitlements gate."""
        async with self.session_factory() as db:
            sub = await SubscriptionRepo(db).active_for_customer(customer_id)
            if sub is None:
                # No subscription => the Free tier's entitlements (gates apply).
                from app.billing.default_catalog import FREE_PLAN

                return project_entitlements(FREE_PLAN, active=False)
            plan_repo = PlanRepo(db)
            plan_row = await plan_repo.get(sub.plan_id)
            prices = await plan_repo.prices_for_plan(sub.plan_id)
            if plan_row is None:  # pragma: no cover - FK guarantees presence
                raise PlanNotFoundError("subscription plan missing")
            catalog_plan = plan_from_row(plan_row, prices)
            active = sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
            return project_entitlements(catalog_plan, active=active)

    # -- usage --------------------------------------------------------------- #

    async def record_usage(
        self,
        *,
        meter: UsageMeter,
        quantity: float,
        subscription_id: str | None = None,
        customer_id: str | None = None,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Record a metered usage event (idempotent). Returns False on a dup key."""
        occurred_at = occurred_at or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            recorded = await UsageRepo(db).record(
                meter=meter,
                quantity=quantity,
                occurred_at=occurred_at,
                subscription_id=subscription_id,
                customer_id=customer_id,
                book_id=book_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                note=note,
            )
            if recorded:
                await self._audit(
                    db,
                    AuditEvent.USAGE_RECORDED,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    meter=meter.value,
                    quantity=quantity,
                )
            return recorded

    async def usage_summary(self, subscription_id: str) -> UsageSummary:
        """Aggregate the current period's usage per meter for a subscription."""
        async with self.session_factory() as db:
            sub = await SubscriptionRepo(db).get(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"no subscription {subscription_id!r}")
            repo = UsageRepo(db)
            summary = UsageSummary(
                period_start=sub.current_period_start, period_end=sub.current_period_end
            )
            for meter in UsageMeter:
                total = await repo.aggregate(
                    meter,
                    MeteredAggregation.SUM,
                    subscription_id=subscription_id,
                    period_start=sub.current_period_start,
                    period_end=sub.current_period_end,
                )
                if total:
                    summary.by_meter[meter] = UsageQuantity(meter, MeteredAggregation.SUM, total, 0)
            return summary

    # -- invoicing + payment ------------------------------------------------- #

    async def generate_period_invoice(
        self, *, subscription_id: str, now: datetime | None = None, finalize: bool = True
    ) -> dict[str, object]:
        """Build (and optionally finalize + charge) the current period's invoice."""
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            sub = await SubscriptionRepo(db).get(subscription_id)
            if sub is None:
                raise SubscriptionNotFoundError(f"no subscription {subscription_id!r}")
            plan_repo = PlanRepo(db)
            plan_row = await plan_repo.get(sub.plan_id)
            prices = await plan_repo.prices_for_plan(sub.plan_id)
            if plan_row is None:  # pragma: no cover
                raise PlanNotFoundError("plan missing")
            catalog_plan = plan_from_row(plan_row, prices)
            price = await plan_repo.get_price(sub.price_id)
            interval = price.interval if price else BillingInterval.MONTH

            usage = await self._usage_summary_in(db, sub)
            coupon = None
            if sub.coupon_code:
                coupon_row = await CouponRepo(db).get_by_code(sub.coupon_code)
                if coupon_row is not None:
                    coupon = coupon_from_row(coupon_row)
            tax_rates = await self._tax_rates_for(db, sub.customer_id, sub.currency)

            ctx = PeriodChargeContext(
                plan=catalog_plan,
                interval=interval,
                usage=usage,
                currency=sub.currency,
                coupon=coupon,
                coupon_period_index=sub.period_index,
                tax_rates=tuple(tax_rates),
                tax_behavior=self.config.tax_behavior,
                at=now,
            )
            draft = build_period_invoice(ctx)
            view = await self._persist_invoice(
                db,
                sub,
                draft,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                now=now,
                finalize=finalize,
            )
            return view

    async def attempt_payment(
        self, *, invoice_id: str, now: datetime | None = None
    ) -> dict[str, object]:
        """Attempt to collect an open invoice and apply the dunning transition."""
        now = now or datetime.now(tz=UTC)
        async with self.session_factory() as db:
            return await self._attempt_payment_in(db, invoice_id, now)

    # -- audit --------------------------------------------------------------- #

    async def audit_trail(self, *, subscription_id: str) -> list[dict[str, object]]:
        async with self.session_factory() as db:
            rows = await AuditRepo(db).for_subscription(subscription_id)
            return [
                {
                    "event": r.event,
                    "occurred_at": r.occurred_at.isoformat(),
                    "actor": r.actor,
                    "amount_minor": r.amount_minor,
                    "detail": r.detail,
                }
                for r in rows
            ]

    # ===================================================================== #
    # internals
    # ===================================================================== #

    @staticmethod
    def _resolve_price(prices: list, price_id: str | None, interval: BillingInterval):  # type: ignore[no-untyped-def]
        if price_id is not None:
            for p in prices:
                if p.id == price_id:
                    return p
            raise PriceNotFoundError(f"no price {price_id!r}")
        # Prefer a flat price on the requested interval, else any flat price.
        from app.billing.enums import PriceType

        flats = [p for p in prices if p.type is PriceType.FLAT]
        for p in flats:
            if p.interval is interval:
                return p
        if flats:
            return flats[0]
        if prices:
            return prices[0]
        raise PriceNotFoundError("plan has no prices")

    async def _usage_summary_in(self, db: AsyncSession, sub: BillingSubscription) -> UsageSummary:
        repo = UsageRepo(db)
        summary = UsageSummary(
            period_start=sub.current_period_start, period_end=sub.current_period_end
        )
        for meter in UsageMeter:
            total = await repo.aggregate(
                meter,
                MeteredAggregation.SUM,
                subscription_id=sub.id,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
            )
            if total:
                summary.by_meter[meter] = UsageQuantity(meter, MeteredAggregation.SUM, total, 0)
        return summary

    async def _tax_rates_for(
        self, db: AsyncSession, customer_id: str | None, currency: str
    ) -> list[TaxRate]:
        if customer_id is None:
            return []
        customer = await CustomerRepo(db).get(customer_id)
        if customer is None:
            return []
        return self.tax_resolver.resolve(customer.tax_country, customer.tax_region)

    def _proration_only_invoice(
        self, proration: ProrationResult, currency: str
    ) -> DraftInvoice:
        from app.billing.invoicing import assemble_invoice
        from app.billing.period_billing import _proration_lines

        lines = _proration_lines(proration)
        return assemble_invoice(lines, currency=currency)

    async def _persist_invoice(
        self,
        db: AsyncSession,
        sub: BillingSubscription,
        draft: DraftInvoice,
        *,
        period_start: datetime | None,
        period_end: datetime | None,
        now: datetime,
        finalize: bool,
    ) -> dict[str, object]:
        repo = InvoiceRepo(db)
        status = InvoiceStatus.DRAFT
        number: str | None = None
        finalized_at: datetime | None = None
        if finalize:
            status = InvoiceStatus.OPEN if draft.total.is_positive else InvoiceStatus.PAID
            number = InvoiceNumberFormatter(self.config.invoice_prefix).format(
                await repo.next_sequence(), year=now.year
            )
            finalized_at = now

        invoice = BillingInvoice(
            subscription_id=sub.id,
            customer_id=sub.customer_id,
            number=number,
            status=status,
            currency=draft.currency,
            subtotal_minor=draft.subtotal.amount_minor,
            discount_minor=draft.discount.amount_minor,
            tax_minor=draft.tax.amount_minor,
            total_minor=draft.total.amount_minor,
            amount_paid_minor=draft.total.amount_minor if status is InvoiceStatus.PAID else 0,
            coupon_code=draft.coupon_code,
            period_start=period_start,
            period_end=period_end,
            finalized_at=finalized_at,
            paid_at=now if status is InvoiceStatus.PAID else None,
        )
        line_rows = [
            BillingInvoiceLine(
                invoice_id="",
                description=line.description,
                amount_minor=line.amount.amount_minor,
                currency=line.amount.currency,
                quantity=line.quantity,
                unit_amount_minor=(
                    line.unit_amount.amount_minor if line.unit_amount is not None else None
                ),
                proration=line.proration,
                price_id=line.price_id,
                meter=line.meter,
            )
            for line in draft.lines
        ]
        await repo.create(invoice, line_rows)
        await self._audit(
            db,
            AuditEvent.INVOICE_CREATED,
            customer_id=sub.customer_id,
            subscription_id=sub.id,
            invoice_id=invoice.id,
            amount_minor=draft.total.amount_minor,
            currency=draft.currency,
        )
        if draft.coupon_code:
            await self._audit(
                db,
                AuditEvent.COUPON_APPLIED,
                customer_id=sub.customer_id,
                subscription_id=sub.id,
                invoice_id=invoice.id,
                code=draft.coupon_code,
            )
        if finalize and number is not None:
            await self._audit(
                db,
                AuditEvent.INVOICE_FINALIZED,
                customer_id=sub.customer_id,
                subscription_id=sub.id,
                invoice_id=invoice.id,
            )
        # Auto-charge a positive, finalized invoice through the provider.
        if finalize and self.config.auto_charge_on_finalize and status is InvoiceStatus.OPEN:
            await self._attempt_payment_in(db, invoice.id, now)
            refreshed = await repo.get(invoice.id)
            if refreshed is not None:
                return self._invoice_view(refreshed)
        return self._invoice_view(invoice)

    async def _attempt_payment_in(
        self, db: AsyncSession, invoice_id: str, now: datetime
    ) -> dict[str, object]:
        invoice_repo = InvoiceRepo(db)
        invoice = await invoice_repo.get(invoice_id)
        if invoice is None:
            from app.billing.errors import InvoiceNotFoundError

            raise InvoiceNotFoundError(f"no invoice {invoice_id!r}")
        if invoice.status not in (InvoiceStatus.OPEN,):
            raise InvalidStateError(f"invoice is {invoice.status.value}, not open")

        sub = (
            await SubscriptionRepo(db).get(invoice.subscription_id)
            if invoice.subscription_id
            else None
        )
        customer = await CustomerRepo(db).get(invoice.customer_id) if invoice.customer_id else None
        amount = Money(invoice.total_minor, invoice.currency)
        attempt_repo = PaymentAttemptRepo(db)
        attempt_number = await attempt_repo.attempt_count(invoice_id) + 1

        # Drive the provider (the fake by default — no real network/payment).
        intent_id: str | None = None
        outcome = PaymentStatus.FAILED
        failure_code: str | None = None
        failure_message: str | None = None
        if customer is not None and customer.provider_customer_id is not None:
            intent = self.provider.create_payment_intent(
                customer_id=customer.provider_customer_id,
                amount=amount,
                invoice_id=invoice_id,
                idempotency_key=f"{invoice_id}:{attempt_number}",
            )
            intent_id = intent.id
            settled = self.provider.confirm_payment_intent(intent.id)
            outcome = settled.status
            failure_code = settled.failure_code
            failure_message = settled.failure_message
        else:
            failure_message = "no payment method on file"

        await self._audit(
            db,
            AuditEvent.PAYMENT_ATTEMPTED,
            customer_id=invoice.customer_id,
            subscription_id=invoice.subscription_id,
            invoice_id=invoice_id,
            amount_minor=amount.amount_minor,
            currency=amount.currency,
            attempt=attempt_number,
        )
        await attempt_repo.record(
            BillingPaymentAttempt(
                invoice_id=invoice_id,
                attempt_number=attempt_number,
                status=outcome,
                amount_minor=amount.amount_minor,
                currency=amount.currency,
                provider_intent_id=intent_id,
                failure_code=failure_code,
                failure_message=failure_message,
                attempted_at=now,
            )
        )

        # Apply the dunning transition.
        state = DunningState(
            schedule=DunningSchedule(retry_days=self.config.dunning_retry_days),
            attempts=attempt_number - 1,
        )
        current_status = sub.status if sub is not None else SubscriptionStatus.ACTIVE
        transition = state.record_attempt(outcome, at=now, current_sub_status=current_status)

        invoice.status = transition.invoice_status
        invoice.attempt_count = attempt_number
        invoice.next_attempt_at = transition.next_retry_at
        if transition.invoice_status is InvoiceStatus.PAID:
            invoice.amount_paid_minor = amount.amount_minor
            invoice.paid_at = now
        await db.flush()

        if sub is not None:
            sub.status = transition.subscription_status
            await db.flush()
            if transition.subscription_status is SubscriptionStatus.UNPAID:
                await CustomerRepo(db).set_delinquent(sub.customer_id, delinquent=True)
            elif transition.subscription_status is SubscriptionStatus.ACTIVE:
                await CustomerRepo(db).set_delinquent(sub.customer_id, delinquent=False)

        if outcome is PaymentStatus.SUCCEEDED:
            await self._audit(
                db,
                AuditEvent.PAYMENT_SUCCEEDED,
                customer_id=invoice.customer_id,
                subscription_id=invoice.subscription_id,
                invoice_id=invoice_id,
                amount_minor=amount.amount_minor,
                currency=amount.currency,
            )
            await self._audit(
                db,
                AuditEvent.INVOICE_PAID,
                customer_id=invoice.customer_id,
                subscription_id=invoice.subscription_id,
                invoice_id=invoice_id,
                amount_minor=amount.amount_minor,
                currency=amount.currency,
            )
        else:
            await self._audit(
                db,
                AuditEvent.PAYMENT_FAILED,
                customer_id=invoice.customer_id,
                subscription_id=invoice.subscription_id,
                invoice_id=invoice_id,
                code=failure_code,
            )
            if transition.exhausted:
                await self._audit(
                    db,
                    AuditEvent.DUNNING_EXHAUSTED,
                    customer_id=invoice.customer_id,
                    subscription_id=invoice.subscription_id,
                    invoice_id=invoice_id,
                )
            elif transition.next_retry_at is not None:
                await self._audit(
                    db,
                    AuditEvent.DUNNING_SCHEDULED,
                    customer_id=invoice.customer_id,
                    subscription_id=invoice.subscription_id,
                    invoice_id=invoice_id,
                    next_attempt_at=transition.next_retry_at.isoformat(),
                )
        return self._invoice_view(invoice)

    async def _audit(
        self,
        db: AsyncSession,
        event: AuditEvent,
        *,
        actor: str | None = None,
        customer_id: str | None = None,
        subscription_id: str | None = None,
        invoice_id: str | None = None,
        amount_minor: int | None = None,
        currency: str | None = None,
        **detail: object,
    ) -> None:
        await AuditRepo(db).record(
            BillingAuditLog(
                event=event.value,
                occurred_at=datetime.now(tz=UTC),
                actor=actor,
                customer_id=customer_id,
                subscription_id=subscription_id,
                invoice_id=invoice_id,
                amount_minor=amount_minor,
                currency=currency,
                detail=dict(detail) or None,
            )
        )

    @staticmethod
    def _sub_view(sub: BillingSubscription) -> dict[str, object]:
        return {
            "id": sub.id,
            "customer_id": sub.customer_id,
            "plan_id": sub.plan_id,
            "price_id": sub.price_id,
            "status": sub.status.value,
            "currency": sub.currency,
            "current_period_start": (
                sub.current_period_start.isoformat() if sub.current_period_start else None
            ),
            "current_period_end": (
                sub.current_period_end.isoformat() if sub.current_period_end else None
            ),
            "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "coupon_code": sub.coupon_code,
        }

    @staticmethod
    def _invoice_view(invoice: BillingInvoice) -> dict[str, object]:
        return {
            "id": invoice.id,
            "number": invoice.number,
            "status": invoice.status.value,
            "currency": invoice.currency,
            "subtotal_minor": invoice.subtotal_minor,
            "discount_minor": invoice.discount_minor,
            "tax_minor": invoice.tax_minor,
            "total_minor": invoice.total_minor,
            "amount_paid_minor": invoice.amount_paid_minor,
            "next_attempt_at": (
                invoice.next_attempt_at.isoformat() if invoice.next_attempt_at else None
            ),
            "attempt_count": invoice.attempt_count,
        }


def _advance_period(start: datetime, interval: BillingInterval) -> datetime:
    """The end of a billing period starting at ``start`` for ``interval``."""
    if interval is BillingInterval.DAY:
        return start + timedelta(days=1)
    if interval is BillingInterval.WEEK:
        return start + timedelta(weeks=1)
    if interval is BillingInterval.MONTH:
        return start + timedelta(days=30)
    return start + timedelta(days=365)


# Re-export a couple of enums callers commonly need alongside the service.
__all__ = ["BillingConfig", "BillingService", "PlanTier"]
