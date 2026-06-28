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
from app.db.models.analytics import (
    AnalyticsDailyRollup,
    AnalyticsEvent,
    AnalyticsSession,
)
from app.db.models.auth import (
    ApiKey,
    AuthAuditLog,
    AuthCredential,
    AuthSession,
    Permission,
    RecoveryCode,
    RefreshToken,
    Role,
    RoleBinding,
    RolePermission,
)
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
    AuthEventType,
    BookStatus,
    EntityType,
    MfaMethod,
    RenderJobStatus,
    RenderPriority,
    SessionMode,
    ShotStatus,
)
from app.db.models.finops import CostKind, CostLedger
from app.db.models.ingest_checkpoint import IngestCheckpoint, IngestMilestone
from app.db.models.integration import (
    AppConnection,
    ConnectionStatus,
    ImportedItem,
    SyncRun,
    SyncRunStatus,
)
from app.db.models.job import JobRun, ScheduledJob
from app.db.models.notification import (
    NotificationDeadLetter,
    NotificationDelivery,
    NotificationInbox,
    NotificationOutbox,
    NotificationPreference,
    WebhookEndpointRow,
)
from app.db.models.pref import Pref
from app.db.models.recommendation import (
    BookFeatureRow,
    BookInteraction,
    UserTasteVector,
)
from app.db.models.render_job import RenderJob
from app.db.models.scene import Scene
from app.db.models.search import SearchDocumentRow, SearchIndexAlias
from app.db.models.session import Session
from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
from app.db.models.user import User

# Additive (reports subsystem): registering the report-artifact index table on
# Base.metadata so Alembic autogenerate + create_all see it. Imported here rather
# than in app.reports to keep the single table-registration entry point.
from app.reports.db_model import ReportArtifact

# Workspaces & teams subsystem (additive; registers its tables on Base.metadata).
from app.workspaces.models import (
    Collection,
    CollectionItem,
    Organization,
    OwnershipTransfer,
    ResourceShare,
    Workspace,
    WorkspaceActivity,
    WorkspaceBook,
    WorkspaceInvitation,
    WorkspaceMember,
)

# Content-translation subsystem tables (app.translation). Imported here so they
# register on ``Base.metadata`` for Alembic autogenerate + relationship
# resolution, exactly like every other aggregate. Additive: the translation
# package owns these definitions; this is only the registration point.
from app.translation.artifacts import (
    ArtifactStatus,
    ReviewStatus,
    TranslationArtifact,
    TranslationGlossaryRow,
    TranslationReview,
    TranslationSegment,
)

# --- Additive: content-moderation & safety subsystem (app.moderation, §9/§10) ---
# Importing here registers the moderation tables on Base.metadata so Alembic
# autogenerate and create_all see them. The models live under app.moderation to
# keep the safety domain self-contained; this import is the single additive hook.
from app.moderation.models import (
    ModerationAuditEntry,
    ModerationEvent,
    ModerationTenantPolicy,
    ReviewItem,
    ViolationCounter,
)

# Feature-flags & experimentation platform tables (app.flags). Imported here so
# Alembic autogenerate + create_all register them on Base.metadata.
from app.flags.db_models import (
    FeatureFlag,
    FlagAudit,
    FlagExperiment,
    FlagExposure,
)

__all__ = [
    "AnalyticsDailyRollup",
    "AnalyticsEvent",
    "AnalyticsSession",
    "AppConnection",
    "ApiKey",
    "ArtifactStatus",
    "AuditAction",
    "AuthAuditLog",
    "AuthCredential",
    "AuthEventType",
    "AuthSession",
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
    "BookFeatureRow",
    "BookInteraction",
    "BranchStatus",
    "Book",
    "BookStatus",
    "BudgetKind",
    "BudgetLedger",
    "CanonAudit",
    "CanonBranch",
    "Collection",
    "CollectionItem",
    "ConnectionStatus",
    "ContinuityState",
    "CostKind",
    "CostLedger",
    "Defect",
    "Entity",
    "EntityType",
    "FeatureFlag",
    "FlagAudit",
    "FlagExperiment",
    "FlagExposure",
    "IngestCheckpoint",
    "IngestMilestone",
    "ImportedItem",
    "JobRun",
    "MfaMethod",
    "ModerationAuditEntry",
    "ModerationEvent",
    "ModerationTenantPolicy",
    "NotificationDeadLetter",
    "NotificationDelivery",
    "NotificationInbox",
    "NotificationOutbox",
    "NotificationPreference",
    "Organization",
    "OwnershipTransfer",
    "Page",
    "Permission",
    "Pref",
    "RecoveryCode",
    "RefreshToken",
    "RenderJob",
    "WebhookEndpointRow",
    "RenderJobStatus",
    "RenderPriority",
    "ReportArtifact",
    "ResourceShare",
    "Role",
    "RoleBinding",
    "RolePermission",
    "ReviewStatus",
    "ReviewItem",
    "Scene",
    "ScheduledJob",
    "SearchDocumentRow",
    "SearchIndexAlias",
    "Session",
    "SessionMode",
    "Shot",
    "ShotCache",
    "ShotStatus",
    "SourceSpanIndex",
    "SyncRun",
    "SyncRunStatus",
    "TranslationArtifact",
    "TranslationGlossaryRow",
    "TranslationReview",
    "TranslationSegment",
    "User",
    "UserTasteVector",
    "ViolationCounter",
    "Workspace",
    "WorkspaceActivity",
    "WorkspaceBook",
    "WorkspaceInvitation",
    "WorkspaceMember",
]
