"""add shot continuity_directive

Revision ID: 8efd633ed2b8
Revises: e260e220ddf0
Create Date: 2026-07-04 18:30:09.462433

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8efd633ed2b8'
down_revision: str | None = 'e260e220ddf0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Task 11: snapshot a shot's planning-time ContinuityDirective (§9.6) so the
    # review-export long-range continuity audit (book_continuity_audit.py) has
    # real persisted wardrobe/setting/lighting/time_of_day/hand-off data to read
    # instead of only in-memory planning state. Nullable with no server default,
    # so every existing shot row reads back as None (no directive) — unchanged.
    op.add_column(
        'shots',
        sa.Column('continuity_directive', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('shots', 'continuity_directive')
