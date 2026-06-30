"""Additive ORM tables for the tenancy isolation layer.

These tables back the :mod:`app.tenancy.domain` repositories in production. They
are **additive** and live under a distinct ``tenant_*`` table namespace so they
never collide with the collaboration-shelf tables in :mod:`app.workspaces`
(``organizations`` / ``workspaces`` / ``workspace_members``). Importing this
module only *adds* tables to ``Base.metadata``; it mutates nothing existing.

Every tenant-owned row carries a ``tenant_key`` column (``org:<id>`` / ``ws:<id>``)
so the :mod:`app.tenancy.guard` query guard has a uniform column to filter on —
this is what lets a single ``tenant_scoped`` call isolate any future scoped table.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.tenancy.roles import Role


class TenantOrg(StrIdMixin, TimestampMixin, Base):
    """A top-level tenant (organization) owning seats, a plan, and a quota envelope."""

    __tablename__ = "tenant_orgs"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    plan: Mapped[str] = mapped_column(String(64), default="free", nullable=False)
    seats: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    #: Quota envelope caps (``0`` == unlimited-by-envelope).
    max_books: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    monthly_video_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    config_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )


class TenantWorkspace(StrIdMixin, TimestampMixin, Base):
    """A workspace nested under a tenant org; may tighten the org envelope."""

    __tablename__ = "tenant_workspaces"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_tenant_workspaces_org_slug"),
        Index("ix_tenant_workspaces_org_id", "org_id"),
        # The isolation key column the query guard filters on.
        Index("ix_tenant_workspaces_tenant_key", "tenant_key"),
    )

    org_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: ``ws:<id>`` — denormalised so the guard has a uniform tenant column.
    tenant_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    #: ``NULL`` caps inherit the org envelope; a set cap tightens it.
    max_books: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_video_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    config_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TenantMembership(StrIdMixin, TimestampMixin, Base):
    """A (user, tenant) edge carrying a role. ``workspace_id`` NULL == org-level."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "org_id",
            "workspace_id",
            name="uq_tenant_memberships_user_org_workspace",
        ),
        Index("ix_tenant_memberships_user_id", "user_id"),
        Index("ix_tenant_memberships_org_id", "org_id"),
        Index("ix_tenant_memberships_workspace_id", "workspace_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        ForeignKey("tenant_orgs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant_workspaces.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[Role] = mapped_column(
        String(32), default=Role.VIEWER.value, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)


class TenantUsage(StrIdMixin, TimestampMixin, Base):
    """Per-tenant consumed usage for the current billing period (quota ledger)."""

    __tablename__ = "tenant_usage"
    __table_args__ = (
        UniqueConstraint("tenant_key", "period", name="uq_tenant_usage_tenant_period"),
        Index("ix_tenant_usage_tenant_key", "tenant_key"),
    )

    #: ``org:<id>`` / ``ws:<id>`` — the guard's tenant column.
    tenant_key: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Billing period bucket (e.g. ``2026-06``); ``"all"`` for non-periodic counts.
    period: Mapped[str] = mapped_column(String(16), default="all", nullable=False)
    books: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    video_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


__all__ = [
    "TenantMembership",
    "TenantOrg",
    "TenantUsage",
    "TenantWorkspace",
]
