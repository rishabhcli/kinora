"""entities(book_id, entity_key, valid_from_beat, valid_to_beat) validity index

Revision ID: f1c2a3b4d5e6
Revises: d9e2f4a6b8c1
Create Date: 2026-06-26

The canon time-travel read (``EntityRepo.get_as_of_beat`` / the batched
``get_present_as_of_beat``, §8.4) filters
``WHERE book_id = ? AND entity_key (IN ?) AND valid_from_beat <= beat
AND (valid_to_beat IS NULL OR valid_to_beat >= beat)`` and picks the highest
version. The existing ``ix_entities_book_key (book_id, entity_key)`` finds a
key's rows but leaves the validity-interval filter to a scan over every version
of that key. This composite extends the prefix with the interval columns so the
planner can resolve the active version directly. Purely additive + reversible;
entity-version writes are rare relative to reads.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f1c2a3b4d5e6"
down_revision: str | None = "d9e2f4a6b8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_entities_key_valid",
        "entities",
        ["book_id", "entity_key", "valid_from_beat", "valid_to_beat"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_entities_key_valid", table_name="entities")
