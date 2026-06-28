# Billing & payments — `backend/app/billing/`

> Living roadmap for the subscription/billing backend. New, self-contained domain.
> Touches the shared seams **additively only**: `core/config.py`,
> `composition.py`, `api/routes/__init__.py`, `db/models/__init__.py`.
> DB tables ship as a single Alembic migration stacked on head `a1b2c3d4e5f6`.

## Why this exists

Kinora's scarce currency is **video-seconds** (kinora.md §11). The budget ledger
(`app/db/models/budget.py`, `app/memory/budget_service.py`) already meters the
*provider-side* cost as an append-only reserve/commit/release ledger. Billing is
the **commercial mirror** of that idea: it meters what the *reader* consumes
(reading-minutes, render-seconds) and what the *plan* entitles them to, then
turns it into subscriptions, invoices, and money — all behind a payment-provider
abstraction with a **fake in-memory transport** so we make zero real
Stripe/network/payment calls anywhere (tests included).

The design borrows the budget ledger's discipline: **append-only, idempotent,
windowed-sum aggregation, no mutated counters.**

## Hard constraints

- `KINORA_LIVE_VIDEO` stays OFF; zero credits spent.
- No real Stripe/network/payment calls — ever. The default provider transport is
  an in-memory fake. A real Stripe transport is *shaped* (interface + DTOs) but
  never wired to a network client.
- Additive-only on shared files; isolated DB `kinora_billing_test` on :5433;
  skip cleanly when the test DB env var is unset.
- Do not edit the budget/finops domains — only *read* them conceptually.

## Module map

| Module | Responsibility |
|---|---|
| `money.py` | Integer-minor-unit `Money`, `Currency`, rounding, allocation (remainder-safe split). |
| `enums.py` | Billing enums (interval, subscription/invoice status, provider event types, etc.). |
| `errors.py` | Billing-domain exception hierarchy. |
| `catalog.py` | Plan + price catalog: features, metered prices, tiered/graduated pricing math. |
| `proration.py` | Time-proration + upgrade/downgrade credit math (pure functions). |
| `coupons.py` | Coupon/discount math: percent / fixed, duration (once/forever/repeating). |
| `tax.py` | Tax computation (inclusive/exclusive, multi-rate, jurisdiction resolver). |
| `metering.py` | Usage event recording + aggregation (reading-minutes / render-seconds). |
| `entitlements.py` | Plan → entitlements projection + feature gating + quota checks. |
| `invoicing.py` | Invoice line assembly, discount/tax/total math, invoice number sequencing. |
| `dunning.py` | Failed-payment retry schedule + state machine. |
| `audit.py` | Append-only billing audit ledger writer. |
| `subscriptions.py` | Subscription lifecycle: trials, activation, upgrade/downgrade, cancel. |
| `provider/` | Payment-provider abstraction: protocol + DTOs + fake in-memory transport + (shaped, unwired) Stripe transport + signed-webhook verify. |
| `webhooks.py` | Idempotent inbound webhook handler (signature verify → event apply). |
| `service.py` | `BillingService` — the orchestration facade the API + composition use. |
| `repositories.py` | Async SQLAlchemy repos over the billing tables. |
| `models.py` | ORM models for the billing tables. |
| `schemas.py` | API DTOs (request/response). |
| `routes.py` | FastAPI router (mounted additively in `api/routes/__init__.py`). |

## Tables (one migration, head `a1b2c3d4e5f6`)

- `billing_plans` — plan catalog (code, name, tier, trial days, active).
- `billing_prices` — prices under a plan (interval, currency, unit amount, metered model, tiers JSON).
- `billing_customers` — user ↔ provider-customer mapping + default currency.
- `billing_subscriptions` — subscription rows (plan/price, status, periods, trial, cancel_at_period_end).
- `billing_subscription_items` — per-price items on a subscription (quantity / metered).
- `billing_usage_records` — append-only metered usage events (idempotent on event key).
- `billing_invoices` — invoice header (status, totals, currency, period).
- `billing_invoice_lines` — invoice line items (description, qty, unit, amount, proration flag).
- `billing_coupons` — coupon definitions (percent/amount, duration).
- `billing_payment_attempts` — dunning/retry attempts against an invoice.
- `billing_webhook_events` — received provider events (idempotency + replay guard).
- `billing_audit_log` — append-only audit ledger of every billing mutation.

## Milestones / phases

- [x] **P0 — primitives**: `money`, `enums`, `errors` + tests.
- [x] **P1 — catalog & pricing math**: `catalog`, tiered/graduated/metered price eval + tests.
- [x] **P2 — proration / coupons / tax**: pure math modules + tests.
- [x] **P3 — metering**: usage recording + windowed aggregation (mirrors budget ledger) + tests.
- [x] **P4 — entitlements & gating**: plan → entitlements, feature gates, quota checks + tests.
- [x] **P5 — invoicing**: line assembly + discount/tax/total + invoice numbering + tests.
- [x] **P6 — provider abstraction**: protocol, DTOs, fake transport, signed webhooks + tests.
- [x] **P7 — dunning**: retry schedule + state machine + tests.
- [x] **P8 — audit ledger** + tests.
- [x] **P9 — persistence**: ORM models + repositories + Alembic migration.
- [x] **P10 — subscriptions lifecycle**: trials/activation/upgrade-downgrade/cancel + tests.
- [x] **P11 — webhooks handler**: idempotent inbound event apply + tests.
- [x] **P12 — BillingService facade** + tests.
- [x] **P13 — API routes + schemas**, mounted additively + tests.
- [x] **P14 — composition wiring** (additive seam on Container) + config settings (additive).

## Additive shared-file changes (documented here per the rules)

- `core/config.py` — append a `# --- Billing ---` block of settings (currency,
  trial days defaults, dunning schedule, webhook secret, provider name). Purely
  additive; no existing field touched.
- `composition.py` — append a lazy `billing_service` accessor + a fake provider
  transport default seam. No existing wiring touched.
- `api/routes/__init__.py` — append `billing` to the imports + `ROUTERS` list.
- `db/models/__init__.py` — append billing model imports + `__all__` entries.

## Status

All phases P0–P14 are implemented and tested. `make lint` (ruff + mypy) is green
across the repo; the billing suite is **199 tests** (162 pure-logic unit + 37
integration). Integration tests run against the isolated `kinora_billing_test`
DB on :5433 and **skip cleanly** when `KINORA_BILLING_TEST_DATABASE_URL`
(falling back to `KINORA_TEST_DATABASE_URL`) is unset. Migration
`b1110a9c5e7d` is the single Alembic head; it applies cleanly and matches the
ORM column-for-column. No real Stripe/network/payment call is made anywhere.

## Remaining roadmap (intentionally deferred — out of scope for this pass)

- **Renewal scheduler.** `SubscriptionRepo.due_for_renewal` is implemented; a
  periodic worker that calls `generate_period_invoice` for subscriptions whose
  period has ended would complete the recurring loop (mirror the idle-sweeper).
- **Render-pipeline → metering bridge.** `metering.render_seconds_event` is the
  hook; wiring the render pipeline's accepted-shot commit to also emit a
  RENDER_SECONDS usage event would tie billing to the live §11 budget commit.
  Left unwired so this pass touches no other domain.
- **Real Stripe transport.** `provider/stripe.py` is the shaped contract;
  implementing it against the Stripe SDK is a separate, explicit decision and
  must stay off by default.
- **Coupon redemption increment on invoice finalize** (the counter column +
  `CouponRepo.increment_redeemed` exist; calling it from `_persist_invoice` on a
  successful coupon application is a small follow-up).

## Conventions followed

- Append-only ledgers; windowed-sum aggregation (mirrors `budget_ledger`).
- Enums as portable VARCHAR + named CHECK via `str_enum` (`db/models/enums.py`).
- Repos `flush`, never `commit`; the unit-of-work owns the transaction.
- All money is integer minor units; never float arithmetic on money.
- Pure functions for all math (proration/tax/coupons/tiers) — trivially testable,
  no I/O.
- No network anywhere; the provider transport is a fake by default.
</content>
