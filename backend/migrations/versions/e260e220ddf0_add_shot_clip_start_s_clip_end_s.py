"""add shot clip_start_s/clip_end_s

Revision ID: e260e220ddf0
Revises: 401ac87abdd2
Create Date: 2026-07-04 17:14:03.779350

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e260e220ddf0'
down_revision: str | None = '401ac87abdd2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Task 8: carry an in-merged-clip [start, end) offset (seconds) on each
    # shot, for shots that are one take within a merged multi-shot event clip.
    # Both nullable with no server default, so every existing shot row reads
    # back as (None, None) — a normal single-shot clip — unchanged.
    op.add_column('shots', sa.Column('clip_start_s', sa.Float(), nullable=True))
    op.add_column('shots', sa.Column('clip_end_s', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('shots', 'clip_end_s')
    op.drop_column('shots', 'clip_start_s')
