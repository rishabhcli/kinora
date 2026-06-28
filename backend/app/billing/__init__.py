"""Kinora billing & payments domain.

A self-contained subscription/billing backend: plan + price catalog,
subscriptions (trials/proration/upgrade-downgrade), entitlements + feature
gating, usage-based metering (reading-minutes / render-seconds), invoice
generation with tax/discount/coupon math, a payment-provider abstraction behind
a **fake in-memory transport** (no real Stripe/network calls anywhere), signed
idempotent inbound-webhook handling, dunning/retry on failed payment, and a full
append-only audit ledger.

This package is the *commercial mirror* of the video-seconds budget ledger
(kinora.md §11): the budget meters provider cost; billing meters reader
consumption and turns it into money. See ``DESIGN.md`` for the roadmap.

Nothing here spends credits, calls a network, or flips ``KINORA_LIVE_VIDEO``.
"""

from __future__ import annotations

from app.billing.money import Money

__all__ = ["Money"]
