"""All ORM models.

Importing this package registers every table on ``Base.metadata`` — Alembic's
``env.py`` imports it so autogenerate sees the full schema, and the app imports
it so ``create_all``/relationship resolution work.
"""

from __future__ import annotations

# --- Billing domain models (additive; registers billing_* tables on metadata) ---
# Imported AFTER the core models so app.db.models.enums (used by billing/models)
# is fully initialized — avoids a package-init circular import.
from app.billing.models import (  # noqa: E402
    BillingAuditLog,
    BillingCoupon,
    BillingCustomer,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPaymentAttempt,
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    BillingSubscriptionItem,
    BillingUsageRecord,
    BillingWebhookEvent,
)
from app.db.base import Base
from app.db.models.beat import Beat
from app.db.models.bitemporal import (
    AuditAction,
    BitemporalState,
    BranchStatus,
    CanonAudit,
    CanonBranch,
)
from app.db.models.book import Book, Page
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.models.continuity import ContinuityState
from app.db.models.defect import Defect
from app.db.models.entity import Entity
from app.db.models.enums import (
    BookStatus,
    EntityType,
    RenderJobStatus,
    RenderPriority,
    SessionMode,
    ShotStatus,
)
from app.db.models.pref import Pref
from app.db.models.render_job import RenderJob
from app.db.models.scene import Scene
from app.db.models.session import Session
from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
from app.db.models.user import User

__all__ = [
    "AuditAction",
    "Base",
    "Beat",
    "BillingAuditLog",
    "BillingCoupon",
    "BillingCustomer",
    "BillingInvoice",
    "BillingInvoiceLine",
    "BillingPaymentAttempt",
    "BillingPlan",
    "BillingPrice",
    "BillingSubscription",
    "BillingSubscriptionItem",
    "BillingUsageRecord",
    "BillingWebhookEvent",
    "BitemporalState",
    "BranchStatus",
    "Book",
    "BookStatus",
    "BudgetKind",
    "BudgetLedger",
    "CanonAudit",
    "CanonBranch",
    "ContinuityState",
    "Defect",
    "Entity",
    "EntityType",
    "Page",
    "Pref",
    "RenderJob",
    "RenderJobStatus",
    "RenderPriority",
    "Scene",
    "Session",
    "SessionMode",
    "Shot",
    "ShotCache",
    "ShotStatus",
    "SourceSpanIndex",
    "User",
]
