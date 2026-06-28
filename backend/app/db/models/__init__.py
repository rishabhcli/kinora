"""All ORM models.

Importing this package registers every table on ``Base.metadata`` — Alembic's
``env.py`` imports it so autogenerate sees the full schema, and the app imports
it so ``create_all``/relationship resolution work.
"""

from __future__ import annotations

from app.db.base import Base
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
from app.db.models.ingest_checkpoint import IngestCheckpoint, IngestMilestone
from app.db.models.integration import (
    AppConnection,
    ConnectionStatus,
    ImportedItem,
    SyncRun,
    SyncRunStatus,
)
from app.db.models.job import JobRun, ScheduledJob
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

__all__ = [
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
    "Defect",
    "Entity",
    "EntityType",
    "IngestCheckpoint",
    "IngestMilestone",
    "ImportedItem",
    "JobRun",
    "MfaMethod",
    "ModerationAuditEntry",
    "ModerationEvent",
    "ModerationTenantPolicy",
    "Organization",
    "OwnershipTransfer",
    "Page",
    "Permission",
    "Pref",
    "RecoveryCode",
    "RefreshToken",
    "RenderJob",
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
