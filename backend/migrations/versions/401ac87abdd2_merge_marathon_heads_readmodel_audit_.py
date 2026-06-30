"""merge marathon heads (readmodel+audit+tenancy)

Revision ID: 401ac87abdd2
Revises: audit_0001, readmodel_proj_0001, tenancy_0001
Create Date: 2026-06-30 05:45:59.336821

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '401ac87abdd2'
down_revision: str | None = ('audit_0001', 'readmodel_proj_0001', 'tenancy_0001')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
