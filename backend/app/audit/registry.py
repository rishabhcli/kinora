"""Single table-registration hook for the audit subsystem.

Importing this module registers :class:`~app.audit.db_models.AuditLogEntry` and
:class:`~app.audit.db_models.AuditCheckpoint` on ``Base.metadata`` so Alembic
autogenerate and ``create_all`` see them. The subsystem owns its own registration
point (rather than editing the shared ``app.db.models`` registry) to stay fully
additive and self-contained — the migration ``audit_0001`` is the authoritative
DDL either way.
"""

from __future__ import annotations

from app.audit.db_models import AuditCheckpoint, AuditLogEntry

__all__ = ["AuditCheckpoint", "AuditLogEntry"]
