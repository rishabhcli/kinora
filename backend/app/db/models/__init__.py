"""All ORM models.

Importing this package registers every table on ``Base.metadata`` — Alembic's
``env.py`` imports it so autogenerate sees the full schema, and the app imports
it so ``create_all``/relationship resolution work.
"""

from __future__ import annotations

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

__all__ = [
    "AuditAction",
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
    "ComplianceLedgerEntry",
    "ConsentPolicy",
    "ConsentRecord",
    "ContinuityState",
    "DSAREvent",
    "DSARRequest",
    "Defect",
    "Entity",
    "EntityType",
    "LegalHold",
    "Page",
    "Pref",
    "RenderJob",
    "RenderJobStatus",
    "RenderPriority",
    "RetentionRule",
    "Scene",
    "Session",
    "SessionMode",
    "Shot",
    "ShotCache",
    "ShotStatus",
    "SourceSpanIndex",
    "User",
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


def __getattr__(name: str) -> object:  # PEP 562 module-level lazy attribute
    if name in _COMPLIANCE_NAMES:
        from app.compliance.db import models as _compliance_models

        return getattr(_compliance_models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
