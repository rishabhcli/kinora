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
from app.db.models.llmops import (
    LLMOpsChangelog,
    LLMOpsEvalReport,
    LLMOpsPromptVersion,
    LLMOpsRun,
)
from app.db.models.mldata import (
    MLDataDatasetVersion,
    MLDataExample,
    MLDataLineageEdge,
)
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

# Additive (durable-workflow engine): register the five workflow-engine tables on
# Base.metadata so Alembic autogenerate + create_all see them (migration
# ``workflows_0001``). Distinct from the jobs framework's scheduled_jobs/job_runs.
from app.db.models.workflow import (
    WorkflowActivityTaskRow,
    WorkflowEventRow,
    WorkflowExecutionRow,
    WorkflowTaskRow,
    WorkflowTimerRow,
)

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

# Sandboxed plugin/extension platform tables (app.platform.plugins). Imported
# here so Alembic autogenerate + create_all register them on Base.metadata.
# Additive only; the platform package owns these definitions.
from app.platform.plugins.db_models import (
    PluginAuditRow,
    PluginInstallationRow,
    PluginRatingRow,
    PluginRegistryEntry,
    PluginReviewRow,
)

# Compliance subsystem tables (additive). Imported LAST, for *side effect only*
# (a bare module import, not ``from ... import names``): this registers the seven
# compliance tables on ``Base.metadata`` so Alembic autogenerate and
# ``create_all`` see them. A bare module import is re-entrancy-safe — when a
# compliance module is itself the import entry point, this line finds the
# (partially initialised) module already in ``sys.modules`` and returns without
# touching its not-yet-defined classes, so the import cycle resolves cleanly.
# The class names are exposed lazily via ``__getattr__`` (PEP 562) below so that
# re-export never forces those classes to exist mid-cycle. See
# app/compliance/DESIGN.md.
import app.compliance.db.models  # noqa: E402, F401  (side-effect: table registration)

# Media subsystem (app.media) — registered here so Alembic autogenerate and the
# app's metadata see the media_assets table. Additive only (Media domain).
from app.media.kinds import MediaAssetKind
from app.media.models import MediaAsset

# Event-sourcing event store (app.eventsourcing.store) — registers the es_*
# tables on Base.metadata for Alembic autogenerate + create_all. Additive:
# the eventsourcing package owns these definitions; this is the registration hook.
from app.eventsourcing.store.models import (
    EventStoreCheckpoint,
    EventStoreEvent,
    EventStoreInbox,
    EventStoreOutbox,
    EventStoreSequence,
    EventStoreSnapshot,
)
# Event-sourcing read-model projections (app.eventsourcing.projections, facet C).
# Importing here registers the three ``esproj_*`` tables (read-model rows,
# projection checkpoints, applied-event idempotency ledger) on Base.metadata so
# Alembic autogenerate + create_all see them. Additive only: the projections
# package owns these definitions; this is the single registration hook. See
# app/eventsourcing/projections/DESIGN.md.
from app.eventsourcing.projections.models import (
    AppliedEventRecord,
    ProjectionCheckpointRecord,
    ReadModelRecord,
)
# Consolidated datalayer read side (app.datalayer, facet C, productionised) —
# registers the three ``datalayer_*`` tables (read-model rows, projection
# checkpoints, applied-event idempotency ledger) on Base.metadata so Alembic
# autogenerate + create_all see them. Additive only; distinct from the esproj_*
# tables above (this read side reads RecordedEvent straight from the real store).
# Schema applied by revision ``readmodel_proj_0001``.
from app.datalayer.models import (
    DataLayerAppliedEventRecord,
    DataLayerCheckpointRecord,
    DataLayerReadModelRecord,
)
# Distributed saga / process-manager engine (app.distributed.sagas) — registered
# here so Alembic autogenerate + create_all see the saga_instances / saga_steps /
# saga_effects tables. Additive only (distributed-systems facet C).
from app.distributed.sagas.models import (
    SagaEffectRow,
    SagaInstanceRow,
    SagaStepRow,
)
# Streaming CDC data plane (app.streaming.cdc, facet C) — registers the
# cdc_offsets + cdc_view_state tables on Base.metadata so Alembic autogenerate
# and create_all see them. Additive only; schema applied by revision ``cdc_0001``.
from app.streaming.cdc.models import CdcOffset, CdcViewStateRow
# Lakehouse feature store (app.lakehouse.features) — registered here so Alembic
# autogenerate + create_all see the feature_store_* tables. Additive only; these
# tables carry no FK into existing tables (an entity key is an opaque string).
from app.lakehouse.features.db_models import (
    FeatureMaterialization,
    FeatureOfflineRow,
    FeatureViewDef,
)
# Zero-trust data-at-rest crypto subsystem (additive; facet B). Registers the
# five ``crypto_*`` tables (KEK registry, tokenization vault, token access log,
# blind-index companion, rotation jobs) on ``Base.metadata`` so Alembic
# autogenerate + ``create_all`` see them. A *bare* side-effect import (not
# ``from ... import names``) keeps this re-entrancy-safe: when a crypto module is
# itself the import entry point, this finds the partially-initialised module
# already in ``sys.modules`` and returns without forcing its classes to exist
# mid-cycle. The names are re-exported lazily via ``__getattr__`` (PEP 562) below.
# See app/zerotrust/crypto/DESIGN.md.
import app.zerotrust.crypto.models  # noqa: E402, F401  (side-effect: table registration)
# Unified authorization plane (app.platform.authz) — registered here so Alembic
# autogenerate + create_all see the two authz_* tables. Additive only: the plane
# owns these definitions and reads every legacy permission fact through adapters
# over the existing tables, so this never competes with the legacy schema.
from app.platform.authz.db_models import AuthzDecisionLogRow, AuthzRelationTuple

# Multi-tenant isolation layer (app.tenancy) — registers the four additive
# ``tenant_*`` tables (orgs, workspaces, memberships, usage) on Base.metadata so
# Alembic autogenerate + create_all see them. Additive only and under a distinct
# table namespace so it never collides with the collaboration-shelf tables in
# app.workspaces; see app/tenancy/__init__.py.
from app.tenancy.models import (
    TenantMembership,
    TenantOrg,
    TenantUsage,
    TenantWorkspace,
)

__all__ = [
    "AnalyticsDailyRollup",
    "AnalyticsEvent",
    "AnalyticsSession",
    "CdcOffset",
    "CdcViewStateRow",
    "AppConnection",
    "AuthzDecisionLogRow",
    "AuthzRelationTuple",
    "ApiKey",
    "AppliedEventRecord",
    "DataLayerAppliedEventRecord",
    "DataLayerCheckpointRecord",
    "DataLayerReadModelRecord",
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
    "ComplianceLedgerEntry",
    "ConnectionStatus",
    "ConsentPolicy",
    "ConsentRecord",
    "ContinuityState",
    "CostKind",
    "CostLedger",
    "CryptoBlindIndex",
    "CryptoKekRegistry",
    "CryptoRotationJob",
    "CryptoTokenAccessLog",
    "CryptoTokenVault",
    "DSAREvent",
    "DSARRequest",
    "Defect",
    "Entity",
    "EntityType",
    "EventStoreCheckpoint",
    "EventStoreEvent",
    "EventStoreInbox",
    "EventStoreOutbox",
    "EventStoreSequence",
    "EventStoreSnapshot",
    "FeatureFlag",
    "FeatureMaterialization",
    "FeatureOfflineRow",
    "FeatureViewDef",
    "FlagAudit",
    "FlagExperiment",
    "FlagExposure",
    "IngestCheckpoint",
    "IngestMilestone",
    "ImportedItem",
    "JobRun",
    "LLMOpsChangelog",
    "LLMOpsEvalReport",
    "LLMOpsPromptVersion",
    "LLMOpsRun",
    "LegalHold",
    "MLDataDatasetVersion",
    "MLDataExample",
    "MLDataLineageEdge",
    "MediaAsset",
    "MediaAssetKind",
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
    "PluginAuditRow",
    "PluginInstallationRow",
    "PluginRatingRow",
    "PluginRegistryEntry",
    "PluginReviewRow",
    "Pref",
    "ProjectionCheckpointRecord",
    "ReadModelRecord",
    "RecoveryCode",
    "RefreshToken",
    "RenderJob",
    "WebhookEndpointRow",
    "RenderJobStatus",
    "RenderPriority",
    "ReportArtifact",
    "ResourceShare",
    "RetentionRule",
    "Role",
    "RoleBinding",
    "RolePermission",
    "ReviewStatus",
    "ReviewItem",
    "SagaEffectRow",
    "SagaInstanceRow",
    "SagaStepRow",
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
    "TenantMembership",
    "TenantOrg",
    "TenantUsage",
    "TenantWorkspace",
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
    "WorkflowActivityTaskRow",
    "WorkflowEventRow",
    "WorkflowExecutionRow",
    "WorkflowTaskRow",
    "WorkflowTimerRow",
]

#: Compliance model names re-exported lazily (resolved from the already-imported
#: ``app.compliance.db.models`` only on attribute access, so re-export never
#: forces those classes to exist mid import-cycle).
_COMPLIANCE_NAMES = frozenset(
    {
        "ComplianceLedgerEntry",
        "ConsentPolicy",
        "ConsentRecord",
        "DSAREvent",
        "DSARRequest",
        "LegalHold",
        "RetentionRule",
    }
)


#: Zero-trust crypto model names re-exported lazily (resolved from the
#: already-imported ``app.zerotrust.crypto.models`` only on attribute access, so
#: re-export never forces those classes to exist mid import-cycle).
_CRYPTO_NAMES = frozenset(
    {
        "CryptoBlindIndex",
        "CryptoKekRegistry",
        "CryptoRotationJob",
        "CryptoTokenAccessLog",
        "CryptoTokenVault",
    }
)


def __getattr__(name: str) -> object:  # PEP 562 module-level lazy attribute
    if name in _COMPLIANCE_NAMES:
        from app.compliance.db import models as _compliance_models

        return getattr(_compliance_models, name)
    if name in _CRYPTO_NAMES:
        from app.zerotrust.crypto import models as _crypto_models

        return getattr(_crypto_models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
