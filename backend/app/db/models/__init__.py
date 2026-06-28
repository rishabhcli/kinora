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
from app.db.models.pref import Pref
from app.db.models.render_job import RenderJob
from app.db.models.scene import Scene
from app.db.models.session import Session
from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
from app.db.models.user import User

__all__ = [
    "ApiKey",
    "AuditAction",
    "AuthAuditLog",
    "AuthCredential",
    "AuthEventType",
    "AuthSession",
    "Base",
    "Beat",
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
    "MfaMethod",
    "Page",
    "Permission",
    "Pref",
    "RecoveryCode",
    "RefreshToken",
    "RenderJob",
    "RenderJobStatus",
    "RenderPriority",
    "Role",
    "RoleBinding",
    "RolePermission",
    "Scene",
    "Session",
    "SessionMode",
    "Shot",
    "ShotCache",
    "ShotStatus",
    "SourceSpanIndex",
    "User",
]
