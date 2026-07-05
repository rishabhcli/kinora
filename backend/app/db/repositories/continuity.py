"""Repository for versioned continuity states + forgetting (kinora.md §8.5)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, update

from app.db.base import new_id
from app.db.models.continuity import ContinuityState
from app.db.repositories.base import BaseRepository


class ContinuityStateRepo(BaseRepository):
    """Assert, retire (forget), and query beat-scoped continuity facts."""

    async def assert_state(
        self,
        *,
        book_id: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        source_span: dict[str, Any] | None = None,
        state_id: str | None = None,
    ) -> str:
        """Add a versioned fact valid from ``valid_from_beat`` (open-ended)."""
        max_version = (
            await self.session.execute(
                select(func.max(ContinuityState.version)).where(
                    ContinuityState.book_id == book_id,
                    ContinuityState.subject_entity_key == subject_entity_key,
                    ContinuityState.predicate == predicate,
                )
            )
        ).scalar_one_or_none()

        state = ContinuityState(
            id=state_id or new_id(),
            book_id=book_id,
            subject_entity_key=subject_entity_key,
            predicate=predicate,
            object_value=object_value,
            valid_from_beat=valid_from_beat,
            valid_to_beat=None,
            version=(max_version or 0) + 1,
            source_span=source_span,
        )
        self.session.add(state)
        await self.session.flush()
        return state.id

    async def retire_state(self, state_id: str, valid_to_beat: int) -> None:
        """Forgetting: close a fact's validity interval at ``valid_to_beat``.

        Half-open, matching :class:`~app.render.continuity_reasoning.intervals.BeatInterval`
        and the bitemporal engine (:meth:`BitemporalRepo` — ``valid_from_beat <= beat
        < valid_to_beat``): the row is preserved (for backward/time-travel reads) but
        drops out of the active set starting AT ``valid_to_beat`` itself, not just
        strictly after it. This must match :meth:`active_states_at_beat`'s own
        comparison exactly, or a fact and its superseding successor both read as
        active at the exact beat the reasoning engine intended to be the clean
        handoff point (found by independent review, 2026-07-05 — the two used to
        disagree, closed vs. half-open).
        """
        await self.session.execute(
            update(ContinuityState)
            .where(ContinuityState.id == state_id)
            .values(valid_to_beat=valid_to_beat)
        )
        await self.session.flush()

    async def list_for_book(self, book_id: str) -> list[ContinuityState]:
        """Every fact for a book — active **and** retired — for the canon editor's
        inspectable continuity view (§8.5). Ordered by interval start, then version
        so the timeline reads chronologically."""
        stmt = (
            select(ContinuityState)
            .where(ContinuityState.book_id == book_id)
            .order_by(ContinuityState.valid_from_beat, ContinuityState.version)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def active_states_at_beat(
        self, book_id: str, beat: int, *, subject_entity_key: str | None = None
    ) -> list[ContinuityState]:
        """Return only the facts whose interval contains ``beat`` (retired ones excluded).

        Half-open (``valid_from_beat <= beat < valid_to_beat``) — see
        :meth:`retire_state`'s docstring for why this must match its own
        comparison exactly.
        """
        stmt = (
            select(ContinuityState)
            .where(
                ContinuityState.book_id == book_id,
                ContinuityState.valid_from_beat <= beat,
                or_(
                    ContinuityState.valid_to_beat.is_(None),
                    ContinuityState.valid_to_beat > beat,
                ),
            )
            .order_by(ContinuityState.valid_from_beat, ContinuityState.version)
        )
        if subject_entity_key is not None:
            stmt = stmt.where(ContinuityState.subject_entity_key == subject_entity_key)
        return list((await self.session.execute(stmt)).scalars().all())
