"""Preference-learning service — persistent across sessions (kinora.md §8.6).

Every Director edit writes a signal (``upsert``); the Cinematographer reads the
aggregated priors (``get``) into its prompt prior on the *next* session, so the
system directs in the reader's taste without being asked. Aggregation per
``kind`` (pacing / palette / composition / …) picks the highest-weight value as
the prior and reports the total accumulated weight and the number of signals.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.db.models.pref import Pref
from app.db.repositories.pref import PrefsRepo


class PreferencePrior(BaseModel):
    """The aggregated prior for one preference ``kind``."""

    kind: str
    value: dict[str, Any]
    weight: float
    signals: int


class PreferencePriors(BaseModel):
    """All aggregated priors for a (user, book) scope."""

    user_id: str | None = None
    book_id: str | None = None
    priors: dict[str, PreferencePrior] = Field(default_factory=dict)


class PrefsService:
    """Read aggregated priors and nudge them from Director edits."""

    def __init__(self, *, prefs: PrefsRepo) -> None:
        self._prefs = prefs

    async def get(
        self, *, user_id: str | None = None, book_id: str | None = None
    ) -> PreferencePriors:
        """Aggregate preference signals into one prior per kind."""
        rows = await self._prefs.get(user_id=user_id, book_id=book_id)
        by_kind: dict[str, list[Pref]] = {}
        for row in rows:
            by_kind.setdefault(row.kind, []).append(row)

        priors: dict[str, PreferencePrior] = {}
        for kind, items in by_kind.items():
            dominant = max(items, key=lambda pref: pref.weight)
            total = sum(pref.weight for pref in items)
            priors[kind] = PreferencePrior(
                kind=kind, value=dominant.value, weight=total, signals=len(items)
            )
        return PreferencePriors(user_id=user_id, book_id=book_id, priors=priors)

    async def upsert(
        self,
        *,
        kind: str,
        value: dict[str, Any],
        user_id: str | None = None,
        book_id: str | None = None,
        weight_delta: float = 1.0,
    ) -> PreferencePrior:
        """Create or reinforce a preference signal (a Director edit, §8.6)."""
        pref = await self._prefs.upsert_nudge(
            kind=kind,
            value=value,
            user_id=user_id,
            book_id=book_id,
            weight_delta=weight_delta,
        )
        return PreferencePrior(kind=pref.kind, value=pref.value, weight=pref.weight, signals=1)


__all__ = ["PreferencePrior", "PreferencePriors", "PrefsService"]
