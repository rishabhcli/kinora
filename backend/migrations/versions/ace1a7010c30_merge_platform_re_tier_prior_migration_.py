"""merge platform re-tier + prior migration heads

Revision ID: ace1a7010c30
Revises: authzplane_0001, b1110a9c5e7d, b3f7c1d20e94, c0ffeejobs01, c0mp11ance0001, c1d2e3f4a5b6, cdc_0001, d4f7c2a9b1e3, esproj_0001, eventstore_0001, f2a9c4d10e7b, f3a7c9e1b2d4, f7a3b2c19e44, featstore_0001, i7a1b2c3d4e5, mldata_0001, n1f2a3b4c5d6, plugins_0001, r1e2p3o4r5t6, sagas_0001, tokvault_0001, translation_0001, workflows_0001
Create Date: 2026-06-29 14:41:24.710456

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ace1a7010c30'
down_revision: str | None = ('authzplane_0001', 'b1110a9c5e7d', 'b3f7c1d20e94', 'c0ffeejobs01', 'c0mp11ance0001', 'c1d2e3f4a5b6', 'cdc_0001', 'd4f7c2a9b1e3', 'esproj_0001', 'eventstore_0001', 'f2a9c4d10e7b', 'f3a7c9e1b2d4', 'f7a3b2c19e44', 'featstore_0001', 'i7a1b2c3d4e5', 'mldata_0001', 'n1f2a3b4c5d6', 'plugins_0001', 'r1e2p3o4r5t6', 'sagas_0001', 'tokvault_0001', 'translation_0001', 'workflows_0001')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
