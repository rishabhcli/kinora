"""Billing & payments domain: plans, prices, customers, subscriptions, usage,
invoices, coupons, payment attempts, webhook events, audit log.

Revision ID: b1110a9c5e7d
Revises: a1b2c3d4e5f6
Create Date: 2026-06-28

Additive migration that creates the twelve ``billing_*`` tables backing the
subscription/billing backend (kinora.md §11 — the commercial mirror of the
video-seconds budget ledger). It touches **no existing table**. Money is stored
as integer minor units (``*_minor`` BigInteger) + a ``currency`` column, never a
float. Enums are portable ``VARCHAR`` + named ``CHECK`` (``native_enum=False``),
matching the rest of the schema. The usage-record, payment-attempt, webhook, and
audit tables are append-only ledgers.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1110a9c5e7d"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    # --- billing_plans ----------------------------------------------------- #
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "tier",
            _enum("free", "starter", "pro", "studio", "enterprise", name="billing_plan_tier"),
            nullable=False,
        ),
        sa.Column("trial_days", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("plan_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_plans")),
        sa.UniqueConstraint("code", name="uq_billing_plans_code"),
    )
    op.create_index(op.f("ix_billing_plans_code"), "billing_plans", ["code"], unique=False)

    # --- billing_prices ---------------------------------------------------- #
    op.create_table(
        "billing_prices",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column(
            "type",
            _enum("flat", "per_unit", "metered", name="billing_price_type"),
            nullable=False,
        ),
        sa.Column(
            "interval",
            _enum("day", "week", "month", "year", name="billing_price_interval"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("flat_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("unit_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("tiers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "tier_mode",
            _enum("graduated", "volume", name="billing_price_tier_mode"),
            nullable=False,
        ),
        sa.Column(
            "meter",
            _enum(
                "reading_minutes",
                "render_seconds",
                "books_imported",
                "director_edits",
                name="billing_price_meter",
            ),
            nullable=True,
        ),
        sa.Column(
            "aggregation",
            _enum("sum", "max", "last", name="billing_price_aggregation"),
            nullable=False,
        ),
        sa.Column("included_units", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["billing_plans.id"],
            name=op.f("fk_billing_prices_plan_id_billing_plans"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_prices")),
    )
    op.create_index("ix_billing_prices_plan", "billing_prices", ["plan_id"], unique=False)

    # --- billing_customers ------------------------------------------------- #
    op.create_table(
        "billing_customers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_customer_id", sa.String(length=128), nullable=True),
        sa.Column("default_currency", sa.String(length=3), nullable=False),
        sa.Column("tax_country", sa.String(length=2), nullable=True),
        sa.Column("tax_region", sa.String(length=8), nullable=True),
        sa.Column("delinquent", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_billing_customers_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_customers")),
        sa.UniqueConstraint("user_id", name="uq_billing_customers_user_id"),
    )
    op.create_index(
        "ix_billing_customers_provider", "billing_customers", ["provider_customer_id"], unique=False
    )

    # --- billing_subscriptions --------------------------------------------- #
    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("price_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            _enum(
                "trialing",
                "active",
                "past_due",
                "unpaid",
                "canceled",
                "incomplete",
                "incomplete_expired",
                "paused",
                name="billing_subscription_status",
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coupon_code", sa.String(length=64), nullable=True),
        sa.Column("period_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_billing_subscriptions_customer_id_billing_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["billing_plans.id"],
            name=op.f("fk_billing_subscriptions_plan_id_billing_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_id"],
            ["billing_prices.id"],
            name=op.f("fk_billing_subscriptions_price_id_billing_prices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_subscriptions")),
    )
    op.create_index(
        "ix_billing_subscriptions_customer", "billing_subscriptions", ["customer_id"], unique=False
    )
    op.create_index(
        "ix_billing_subscriptions_status", "billing_subscriptions", ["status"], unique=False
    )

    # --- billing_subscription_items ---------------------------------------- #
    op.create_table(
        "billing_subscription_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=False),
        sa.Column("price_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_billing_subscription_items_subscription_id_billing_s_90e5"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["price_id"],
            ["billing_prices.id"],
            name=op.f("fk_billing_subscription_items_price_id_billing_prices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_subscription_items")),
    )
    op.create_index(
        "ix_billing_subscription_items_sub",
        "billing_subscription_items",
        ["subscription_id"],
        unique=False,
    )

    # --- billing_coupons --------------------------------------------------- #
    op.create_table(
        "billing_coupons",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "discount_type",
            _enum("percent", "fixed", name="billing_coupon_discount_type"),
            nullable=False,
        ),
        sa.Column("percent_off", sa.Float(), nullable=True),
        sa.Column("amount_off_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "duration",
            _enum("once", "forever", "repeating", name="billing_coupon_duration"),
            nullable=False,
        ),
        sa.Column("duration_in_periods", sa.Integer(), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("redeemed_count", sa.Integer(), nullable=False),
        sa.Column("redeem_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("min_subtotal_minor", sa.BigInteger(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_coupons")),
        sa.UniqueConstraint("code", name="uq_billing_coupons_code"),
    )
    op.create_index(op.f("ix_billing_coupons_code"), "billing_coupons", ["code"], unique=False)

    # --- billing_invoices -------------------------------------------------- #
    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("number", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            _enum("draft", "open", "paid", "uncollectible", "void", name="billing_invoice_status"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("discount_minor", sa.BigInteger(), nullable=False),
        sa.Column("tax_minor", sa.BigInteger(), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("amount_paid_minor", sa.BigInteger(), nullable=False),
        sa.Column("coupon_code", sa.String(length=64), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_billing_invoices_subscription_id_billing_subscriptions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_billing_invoices_customer_id_billing_customers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_invoices")),
        sa.UniqueConstraint("number", name="uq_billing_invoices_number"),
    )
    op.create_index(
        "ix_billing_invoices_subscription", "billing_invoices", ["subscription_id"], unique=False
    )
    op.create_index("ix_billing_invoices_status", "billing_invoices", ["status"], unique=False)

    # --- billing_invoice_lines --------------------------------------------- #
    op.create_table(
        "billing_invoice_lines",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("proration", sa.Boolean(), nullable=False),
        sa.Column("price_id", sa.String(length=64), nullable=True),
        sa.Column("meter", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["billing_invoices.id"],
            name=op.f("fk_billing_invoice_lines_invoice_id_billing_invoices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_invoice_lines")),
    )
    op.create_index(
        "ix_billing_invoice_lines_invoice", "billing_invoice_lines", ["invoice_id"], unique=False
    )

    # --- billing_usage_records (append-only, idempotent) ------------------- #
    op.create_table(
        "billing_usage_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column(
            "meter",
            _enum(
                "reading_minutes",
                "render_seconds",
                "books_imported",
                "director_edits",
                name="billing_usage_meter",
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("book_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_billing_usage_records_subscription_id_billing_subscriptions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["billing_customers.id"],
            name=op.f("fk_billing_usage_records_customer_id_billing_customers"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_usage_records")),
        sa.UniqueConstraint("idempotency_key", name="uq_billing_usage_records_idempotency_key"),
    )
    op.create_index(
        "ix_billing_usage_records_scope",
        "billing_usage_records",
        ["subscription_id", "meter", "occurred_at"],
        unique=False,
    )

    # --- billing_payment_attempts (append-only dunning history) ------------ #
    op.create_table(
        "billing_payment_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "pending",
                "succeeded",
                "failed",
                "requires_action",
                "canceled",
                name="billing_payment_status",
            ),
            nullable=False,
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_intent_id", sa.String(length=128), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["billing_invoices.id"],
            name=op.f("fk_billing_payment_attempts_invoice_id_billing_invoices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_payment_attempts")),
    )
    op.create_index(
        "ix_billing_payment_attempts_invoice",
        "billing_payment_attempts",
        ["invoice_id"],
        unique=False,
    )

    # --- billing_webhook_events (idempotency + replay guard) --------------- #
    op.create_table(
        "billing_webhook_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_webhook_events")),
        sa.UniqueConstraint(
            "provider", "event_id", name="uq_billing_webhook_events_provider_event"
        ),
    )

    # --- billing_audit_log (append-only) ----------------------------------- #
    op.create_table(
        "billing_audit_log",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("subscription_id", sa.String(length=64), nullable=True),
        sa.Column("invoice_id", sa.String(length=64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_audit_log")),
    )
    op.create_index(
        "ix_billing_audit_log_subscription", "billing_audit_log", ["subscription_id"], unique=False
    )
    op.create_index(
        "ix_billing_audit_log_customer", "billing_audit_log", ["customer_id"], unique=False
    )
    op.create_index("ix_billing_audit_log_event", "billing_audit_log", ["event"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_billing_audit_log_event", table_name="billing_audit_log")
    op.drop_index("ix_billing_audit_log_customer", table_name="billing_audit_log")
    op.drop_index("ix_billing_audit_log_subscription", table_name="billing_audit_log")
    op.drop_table("billing_audit_log")

    op.drop_table("billing_webhook_events")

    op.drop_index("ix_billing_payment_attempts_invoice", table_name="billing_payment_attempts")
    op.drop_table("billing_payment_attempts")

    op.drop_index("ix_billing_usage_records_scope", table_name="billing_usage_records")
    op.drop_table("billing_usage_records")

    op.drop_index("ix_billing_invoice_lines_invoice", table_name="billing_invoice_lines")
    op.drop_table("billing_invoice_lines")

    op.drop_index("ix_billing_invoices_status", table_name="billing_invoices")
    op.drop_index("ix_billing_invoices_subscription", table_name="billing_invoices")
    op.drop_table("billing_invoices")

    op.drop_index(op.f("ix_billing_coupons_code"), table_name="billing_coupons")
    op.drop_table("billing_coupons")

    op.drop_index("ix_billing_subscription_items_sub", table_name="billing_subscription_items")
    op.drop_table("billing_subscription_items")

    op.drop_index("ix_billing_subscriptions_status", table_name="billing_subscriptions")
    op.drop_index("ix_billing_subscriptions_customer", table_name="billing_subscriptions")
    op.drop_table("billing_subscriptions")

    op.drop_index("ix_billing_customers_provider", table_name="billing_customers")
    op.drop_table("billing_customers")

    op.drop_index("ix_billing_prices_plan", table_name="billing_prices")
    op.drop_table("billing_prices")

    op.drop_index(op.f("ix_billing_plans_code"), table_name="billing_plans")
    op.drop_table("billing_plans")
