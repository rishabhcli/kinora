"""Preference-learning service — persistent across sessions (kinora.md §8.6).

Every Director edit writes a signal (``upsert``); the Cinematographer reads the
aggregated priors (``get``) into its prompt prior on the *next* session, so the
system directs in the reader's taste without being asked. Aggregation per
``kind`` (pacing / palette / composition / …) picks the highest-weight value as
the prior and reports the total accumulated weight and the number of signals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.db.models.pref import Pref
from app.memory.prefs_signals import (
    BIAS_CLAMP,
    SIGNAL_STEP,
    decay_factor,
    infer_signals,
    infer_signals_from_changes,
    merge_bias,
)


def _age_seconds(now: datetime, ts: datetime | None) -> float:
    """Seconds between ``now`` and a row's ``updated_at`` (0 when unknown/future)."""
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (now - ts).total_seconds())


def _row_bias(row: Pref) -> float:
    raw = row.value.get("bias") if isinstance(row.value, dict) else None
    return float(raw) if isinstance(raw, (int, float)) else 0.0


class PrefsStore(Protocol):
    """The repository seam :class:`PrefsService` reads/nudges over (kinora.md §8.6).

    :class:`~app.db.repositories.pref.PrefsRepo` satisfies it in production; tests
    inject an in-memory double, so the learning logic is exercised without a DB.
    """

    async def get(
        self, *, user_id: str | None = None, book_id: str | None = None, kind: str | None = None
    ) -> list[Pref]: ...

    async def upsert_nudge(
        self,
        *,
        kind: str,
        value: dict[str, Any],
        user_id: str | None = None,
        book_id: str | None = None,
        weight_delta: float = 1.0,
    ) -> Pref: ...

    async def delete(self, *, user_id: str | None = None, book_id: str | None = None) -> int: ...


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

    def __init__(self, *, prefs: PrefsStore) -> None:
        self._prefs = prefs

    async def get(
        self, *, user_id: str | None = None, book_id: str | None = None
    ) -> PreferencePriors:
        """Aggregate preference signals into one prior per kind."""
        rows = await self._prefs.get(user_id=user_id, book_id=book_id)
        now = datetime.now(UTC)
        by_kind: dict[str, list[Pref]] = {}
        for row in rows:
            by_kind.setdefault(row.kind, []).append(row)

        priors: dict[str, PreferencePrior] = {}
        for kind, items in by_kind.items():
            # Recency-decay each signal (§8.5): a taste you stop expressing fades.
            decays = {id(r): decay_factor(_age_seconds(now, r.updated_at)) for r in items}
            total_weight = round(sum(r.weight * decays[id(r)] for r in items), 4)
            if all(isinstance(r.value, dict) and "bias" in r.value for r in items):
                # Sum decayed biases across books (a taste held in many books is a
                # strong global default; opposing books partially cancel), clamped.
                agg = sum(_row_bias(r) * decays[id(r)] for r in items)
                value: dict[str, Any] = {"bias": max(-BIAS_CLAMP, min(BIAS_CLAMP, round(agg, 4)))}
                # Carry the most-recent row's non-bias metadata (e.g. provenance).
                recent = max(items, key=lambda r: r.updated_at or now)
                value.update({k: v for k, v in (recent.value or {}).items() if k != "bias"})
            else:
                value = max(items, key=lambda r: r.weight * decays[id(r)]).value
            priors[kind] = PreferencePrior(
                kind=kind, value=value, weight=total_weight, signals=len(items)
            )
        return PreferencePriors(user_id=user_id, book_id=book_id, priors=priors)

    async def get_effective(
        self, *, user_id: str, book_id: str
    ) -> PreferencePriors:
        """The directing style the agent should use for ``(user, book)`` (§8.6).

        The book's own learned axes win; the reader's cross-book *global* style
        fills any axis the book hasn't learned yet — so a brand-new book is
        directed in the reader's established taste from the first shot, then
        specialises as they direct it.
        """
        book = await self.get(book_id=book_id)
        glob = await self.get(user_id=user_id)
        merged = {**glob.priors, **book.priors}
        return PreferencePriors(user_id=user_id, book_id=book_id, priors=merged)

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

    async def record_signal(
        self,
        *,
        kind: str,
        direction: int,
        user_id: str | None = None,
        book_id: str | None = None,
        step: float = SIGNAL_STEP,
        note: str | None = None,
    ) -> PreferencePrior:
        """Apply one Director signal: nudge the axis's signed ``bias``, +1 weight.

        Read-modify-write on the single ``(scope, kind)`` row so opposing notes
        cancel and repeated notes reinforce — three "slower" notes accumulate to a
        clearly-applied prior, while a "slower" then "faster" nets out. The
        triggering ``note`` is stored as provenance so the panel can explain *why*
        a prior exists ("from: 'too fast — slow it down'").
        """
        existing = await self._prefs.get(user_id=user_id, book_id=book_id, kind=kind)
        old_bias = 0.0
        if existing:
            raw = existing[0].value.get("bias") if isinstance(existing[0].value, dict) else None
            old_bias = float(raw) if isinstance(raw, (int, float)) else 0.0
        new_bias = merge_bias(old_bias, direction, step)
        value: dict[str, Any] = {"bias": new_bias}
        if note:
            value["note"] = note.strip()[:160]
        pref = await self._prefs.upsert_nudge(
            kind=kind,
            value=value,
            user_id=user_id,
            book_id=book_id,
            weight_delta=1.0,
        )
        return PreferencePrior(kind=pref.kind, value=pref.value, weight=pref.weight, signals=1)

    async def record_note(
        self, note: str, *, user_id: str | None = None, book_id: str | None = None
    ) -> list[PreferencePrior]:
        """Learn from a Director region-comment: infer signals, apply each (§8.6)."""
        return await self._record(
            infer_signals(note), user_id=user_id, book_id=book_id, note=note
        )

    async def record_changes(
        self,
        changes: dict[str, Any],
        *,
        user_id: str | None = None,
        book_id: str | None = None,
    ) -> list[PreferencePrior]:
        """Learn from a canon edit (a re-coloured/re-framed entity, §8.6)."""
        return await self._record(
            infer_signals_from_changes(changes), user_id=user_id, book_id=book_id
        )

    async def _record(
        self,
        signals: list[tuple[str, int]],
        *,
        user_id: str | None,
        book_id: str | None,
        note: str | None = None,
    ) -> list[PreferencePrior]:
        out: list[PreferencePrior] = []
        for kind, direction in signals:
            out.append(
                await self.record_signal(
                    kind=kind, direction=direction, user_id=user_id, book_id=book_id, note=note
                )
            )
        return out

    async def reset(self, *, user_id: str | None = None, book_id: str | None = None) -> int:
        """Clear learned preferences for a scope; return how many were removed.

        ``reset(book_id=b)`` clears one book's learned style; ``reset(user_id=u)``
        is the global reset across all of a reader's books.
        """
        return await self._prefs.delete(user_id=user_id, book_id=book_id)


__all__ = ["PreferencePrior", "PreferencePriors", "PrefsService"]
