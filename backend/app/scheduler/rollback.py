"""Speculative execution with rollback (kinora.md §4.8/§4.6/§12.1).

§4.8 already cancels in-flight speculation on a seek and §4.6 suspends promotion
during a skim. But both are *coarse*: a seek cancels everything beyond a fixed
distance; a skim just stops promoting new work. As the optimiser (Phase 4) makes
the Scheduler more willing to promote *aggressively* when it predicts a stable
reader, we need a precise, auditable way to **undo** a promotion when the
trajectory that justified it turns out to be wrong — without ever over-charging
the budget.

This module is the **speculation ledger**: a per-trajectory record of every
promotion (its job, its reservation, the word it targeted, the token under which
it was committed). When a trajectory is invalidated — a seek away, a skim onset, a
direction flip the dwell counter rejected — the ledger computes the **rollback
set**: exactly which in-flight jobs to cancel and which reservations to release,
*and which to keep* because the new trajectory still passes through them (a
backward seek that lands inside an already-buffered span is a cache hit, §4.8, not
a rollback).

Determinism + the budget invariant
-----------------------------------
The ledger is pure given its entries and the invalidation event. It never spends:
it only *records* reservations made elsewhere and computes *releases*. A correct
rollback releases each cancelled job's reservation **exactly once** (tracked by a
``released`` flag) so a double-cancel can't double-credit, and an entry that is
*kept* is never released — so the budget reflects only what is actually still
in-flight. Promotion itself remains ``can_render_live()``-gated upstream; the
ledger cannot create a reservation, only retire one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.scheduler.zones import DEFAULT_VELOCITY_WPS, eta_seconds


class InvalidationReason(StrEnum):
    """Why a trajectory was invalidated (drives the rollback policy, §4.8/§4.6)."""

    #: The reader jumped (§4.8): keep promotions near the new word, roll back the rest.
    SEEK = "seek"
    #: A rapid skim onset (§4.6): roll back *all* speculative promotions on the token.
    SKIM = "skim"
    #: A dwell-rejected direction flip (§4.7): roll back promotions ahead of the flip.
    DIRECTION_FLIP = "direction_flip"


@dataclass(slots=True)
class SpeculativePromotion:
    """One recorded promotion under a trajectory token (§4.6)."""

    job_id: str
    shot_id: str
    reservation_id: str
    word_index_start: int
    reserved_video_s: float
    trajectory_token: str
    #: Set once its reservation has been released (rollback is idempotent).
    released: bool = False


@dataclass(slots=True)
class RollbackPlan:
    """The precise undo set for an invalidation (§4.8).

    ``cancel`` are the promotions whose jobs should be cancelled + reservations
    released; ``keep`` are those the new trajectory still needs (a cache hit). The
    plan is what a caller hands to the queue's cancel + the budget's release — the
    ledger computes it but performs no I/O.
    """

    cancel: list[SpeculativePromotion] = field(default_factory=list)
    keep: list[SpeculativePromotion] = field(default_factory=list)

    @property
    def reclaimed_video_s(self) -> float:
        """Total reserved video-seconds the cancellations will release."""
        return round(sum(p.reserved_video_s for p in self.cancel if not p.released), 6)

    @property
    def cancel_job_ids(self) -> list[str]:
        return [p.job_id for p in self.cancel]

    @property
    def release_reservation_ids(self) -> list[str]:
        return [p.reservation_id for p in self.cancel if not p.released]


class SpeculationLedger:
    """Records speculative promotions and computes rollbacks (§4.6/§4.8/§12.1).

    One ledger per session. Pure and deterministic; holds only small records, so
    it serialises into the session control state alongside the buffer.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SpeculativePromotion] = {}

    # -- recording ----------------------------------------------------------- #

    def record(
        self,
        *,
        job_id: str,
        shot_id: str,
        reservation_id: str,
        word_index_start: int,
        reserved_video_s: float,
        trajectory_token: str,
    ) -> SpeculativePromotion:
        """Record a promotion (called right after a successful enqueue+reserve)."""
        entry = SpeculativePromotion(
            job_id=job_id,
            shot_id=shot_id,
            reservation_id=reservation_id,
            word_index_start=word_index_start,
            reserved_video_s=reserved_video_s,
            trajectory_token=trajectory_token,
        )
        self._entries[job_id] = entry
        return entry

    def confirm(self, job_id: str) -> None:
        """Drop a promotion the reader has now reached (no longer speculative).

        Once the playhead passes a promoted shot it is *consumed*, not
        speculative — it can never be rolled back, so we stop tracking it (its
        reservation is the worker's to release on completion).
        """
        self._entries.pop(job_id, None)

    @property
    def outstanding(self) -> list[SpeculativePromotion]:
        """All promotions still tracked (not confirmed, not released)."""
        return [e for e in self._entries.values() if not e.released]

    @property
    def outstanding_video_s(self) -> float:
        return round(sum(e.reserved_video_s for e in self.outstanding), 6)

    # -- rollback planning --------------------------------------------------- #

    def plan_rollback(
        self,
        *,
        reason: InvalidationReason,
        new_word: int,
        old_token: str,
        keep_threshold_s: float = 120.0,
        velocity_wps: float = DEFAULT_VELOCITY_WPS,
    ) -> RollbackPlan:
        """Compute the cancel/keep split for a trajectory invalidation (§4.8).

        Policy per reason:
          * ``SEEK`` — keep promotions whose target is within ``keep_threshold_s``
            of reading-time from ``new_word`` (a near/backward seek is a cache hit,
            §4.8); roll back the rest.
          * ``SKIM`` — roll back **all** outstanding promotions on ``old_token``
            (§4.6 suspends the whole speculative path during a skim).
          * ``DIRECTION_FLIP`` — keep promotions *behind* ``new_word`` (already
            read / about to be re-read) and roll back those ahead (the rejected
            forward path).

        Only promotions made under ``old_token`` are considered; a promotion on a
        *newer* token (already re-seeded) is never rolled back by an old event.
        """
        plan = RollbackPlan()
        for entry in self.outstanding:
            if entry.trajectory_token != old_token:
                plan.keep.append(entry)
                continue
            if self._should_keep(reason, entry, new_word, keep_threshold_s, velocity_wps):
                plan.keep.append(entry)
            else:
                plan.cancel.append(entry)
        return plan

    def apply(self, plan: RollbackPlan) -> float:
        """Mark a plan's cancellations released (idempotent) → reclaimed seconds.

        Call after the caller has actually cancelled the jobs + released the
        reservations. Marks each cancelled entry ``released`` exactly once (so a
        re-applied plan reclaims nothing more) and drops them from tracking.
        Returns the video-seconds reclaimed by *this* application.
        """
        reclaimed = 0.0
        for entry in plan.cancel:
            tracked = self._entries.get(entry.job_id)
            if tracked is None or tracked.released:
                continue
            tracked.released = True
            reclaimed += tracked.reserved_video_s
            del self._entries[entry.job_id]
        return round(reclaimed, 6)

    @staticmethod
    def _should_keep(
        reason: InvalidationReason,
        entry: SpeculativePromotion,
        new_word: int,
        keep_threshold_s: float,
        velocity_wps: float,
    ) -> bool:
        if reason is InvalidationReason.SKIM:
            return False  # the whole speculative path is abandoned
        if reason is InvalidationReason.DIRECTION_FLIP:
            # Keep what's behind the flip point (cache hit on re-read); drop ahead.
            return entry.word_index_start <= new_word
        # SEEK: keep what's within the reading-time keep window of the new word.
        eta = abs(eta_seconds(entry.word_index_start, new_word, velocity_wps))
        return eta <= keep_threshold_s

    # -- serialisation (session control state) ------------------------------- #

    def to_state(self) -> list[dict[str, object]]:
        """Dump outstanding entries for persistence (JSON-friendly)."""
        return [
            {
                "job_id": e.job_id,
                "shot_id": e.shot_id,
                "reservation_id": e.reservation_id,
                "word_index_start": e.word_index_start,
                "reserved_video_s": e.reserved_video_s,
                "trajectory_token": e.trajectory_token,
                "released": e.released,
            }
            for e in self._entries.values()
        ]

    @classmethod
    def from_state(cls, state: Iterable[dict[str, object]] | None) -> SpeculationLedger:
        """Rebuild a ledger from persisted entries."""
        ledger = cls()
        for row in state or []:
            entry = SpeculativePromotion(
                job_id=str(row["job_id"]),
                shot_id=str(row["shot_id"]),
                reservation_id=str(row["reservation_id"]),
                word_index_start=int(str(row["word_index_start"])),
                reserved_video_s=float(str(row["reserved_video_s"])),
                trajectory_token=str(row["trajectory_token"]),
                released=bool(row.get("released", False)),
            )
            ledger._entries[entry.job_id] = entry
        return ledger


__all__ = [
    "InvalidationReason",
    "RollbackPlan",
    "SpeculationLedger",
    "SpeculativePromotion",
]
