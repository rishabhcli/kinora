"""Path-invalidation cancellation + refund + cache salvage (kinora.md §4.8).

When the reader's *actual* path diverges from what we speculated — a forward seek
past the speculated span, a backward glance, a chapter jump — the speculations we
launched on the old path are now suspect. This module decides, for each in-flight
speculation, whether to **keep** it (the new path still reaches it) or **cancel**
it, and on cancellation:

* **refund** its reserved speculative dollars to the budget — *but only if the
  render hadn't started* (a started render's seconds are sunk; refunding them
  would double-credit the ledger, exactly the §4.8 hazard the scheduler's
  rollback ledger guards against);
* **salvage** its asset into the cache when a re-hit is plausible (a backward seek
  that lands inside an already-buffered span is a cache hit, not waste).

The ledger tracks each speculation exactly once: a cancelled entry is marked
``released`` so a double-invalidation cannot double-refund. State is small and
in-memory; the engine owns one ledger per session.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.video.speculate.protocols import CacheLookupProtocol, SpeculativeBudgetProtocol
from app.video.speculate.types import CancellationOutcome, SpeculationChoice


class SpeculationStatus(StrEnum):
    """Lifecycle of a launched speculation."""

    #: Reserved + queued, render not yet started → reservation is refundable.
    PENDING = "pending"
    #: Render in flight → seconds are sunk; cancel stops it but does not refund.
    RUNNING = "running"
    #: Asset landed in the cache → a later reach on it is a free hit.
    DONE = "done"
    #: Cancelled (path invalidated). Terminal.
    CANCELLED = "cancelled"


@dataclass(slots=True)
class SpeculationEntry:
    """A single launched speculation tracked by the ledger.

    ``reserved_usd`` is what was put on the speculative budget at launch.
    ``released`` flips exactly once on the refund path so a double-cancel cannot
    double-credit (the §4.8 idempotency guard).
    """

    shot_key: str
    word_start: int
    video_seconds: float
    model_id: str
    reserved_usd: float
    hit_probability: float
    status: SpeculationStatus = SpeculationStatus.PENDING
    released: bool = False

    @classmethod
    def from_choice(cls, choice: SpeculationChoice) -> SpeculationEntry:
        return cls(
            shot_key=choice.shot_key,
            word_start=choice.reach.shot.word_start,
            video_seconds=choice.reach.shot.video_seconds,
            model_id=choice.model_id,
            reserved_usd=choice.cost_usd,
            hit_probability=choice.reach.hit_probability,
        )


class SpeculationLedger:
    """Tracks launched speculations and computes path-invalidation cancellations.

    Pure over its injected budget/cache seams: it calls
    :meth:`SpeculativeBudgetProtocol.release` for refunds and consults
    :meth:`CacheLookupProtocol.is_salvageable` for cache keeps. One ledger per
    reading session; the engine drives it.
    """

    def __init__(
        self,
        budget: SpeculativeBudgetProtocol,
        cache: CacheLookupProtocol,
    ) -> None:
        self._budget = budget
        self._cache = cache
        self._entries: dict[str, SpeculationEntry] = {}

    # -- bookkeeping ------------------------------------------------------- #

    def register(self, choice: SpeculationChoice) -> SpeculationEntry:
        """Record a launched speculation (idempotent on shot_key)."""
        entry = self._entries.get(choice.shot_key)
        if entry is None:
            entry = SpeculationEntry.from_choice(choice)
            self._entries[choice.shot_key] = entry
        return entry

    def mark_running(self, shot_key: str) -> None:
        """The render started — its seconds are now sunk (no refund on cancel)."""
        entry = self._entries.get(shot_key)
        if entry is not None and entry.status is SpeculationStatus.PENDING:
            entry.status = SpeculationStatus.RUNNING
            # The reservation becomes realised spend once work begins.
            self._budget.settle(entry.reserved_usd)

    def mark_done(self, shot_key: str) -> None:
        """The asset landed — a later reach on it is a free cache hit."""
        entry = self._entries.get(shot_key)
        if entry is not None and entry.status in (
            SpeculationStatus.PENDING,
            SpeculationStatus.RUNNING,
        ):
            if entry.status is SpeculationStatus.PENDING:
                self._budget.settle(entry.reserved_usd)
            entry.status = SpeculationStatus.DONE

    @property
    def active(self) -> list[SpeculationEntry]:
        """Entries still occupying buffer state (pending/running/done)."""
        return [
            e
            for e in self._entries.values()
            if e.status is not SpeculationStatus.CANCELLED
        ]

    def entry(self, shot_key: str) -> SpeculationEntry | None:
        return self._entries.get(shot_key)

    # -- invalidation ------------------------------------------------------ #

    def invalidate(
        self,
        *,
        new_focus_word: int,
        keep_horizon_words: int,
    ) -> CancellationOutcome:
        """Cancel speculations the new reading position no longer reaches (§4.8).

        A speculation is **kept** when its shot sits in the forward window
        ``[new_focus_word, new_focus_word + keep_horizon_words]`` — the new
        trajectory still plausibly reaches it. Everything else is **cancelled**:

        * a ``PENDING`` (unstarted) entry refunds its reservation to the budget;
        * a ``RUNNING`` entry's seconds are sunk (no refund) but the render is
          stopped going forward;
        * a ``DONE`` or salvageable entry is offered to the cache for a re-hit.

        Idempotent: an already-released entry never refunds twice.
        """
        keep_lo = new_focus_word
        keep_hi = new_focus_word + max(0, keep_horizon_words)
        cancelled: list[str] = []
        salvaged: list[str] = []
        kept: list[str] = []
        refunded = 0.0

        for entry in list(self._entries.values()):
            if entry.status is SpeculationStatus.CANCELLED:
                continue
            in_window = keep_lo <= entry.word_start <= keep_hi
            if in_window:
                kept.append(entry.shot_key)
                continue
            refunded += self._cancel(entry, cancelled, salvaged)

        return CancellationOutcome(
            cancelled=cancelled,
            refunded_usd=round(refunded, 6),
            salvaged=salvaged,
            kept=kept,
        )

    def _cancel(
        self,
        entry: SpeculationEntry,
        cancelled: list[str],
        salvaged: list[str],
    ) -> float:
        """Cancel one entry: refund-if-unstarted, salvage-if-cacheable (idempotent).

        Returns the dollars refunded for this entry (0 unless it was an unstarted,
        not-yet-released reservation), appending to the running cancel/salvage lists.
        """
        # Salvage decision first (uses the pre-cancel status).
        salvage = entry.status is SpeculationStatus.DONE or self._cache.is_salvageable(
            entry.shot_key
        )
        # Refund only an unstarted reservation, exactly once.
        refunded = 0.0
        if entry.status is SpeculationStatus.PENDING and not entry.released:
            self._budget.release(entry.reserved_usd)
            entry.released = True
            refunded = entry.reserved_usd

        entry.status = SpeculationStatus.CANCELLED
        cancelled.append(entry.shot_key)
        if salvage:
            salvaged.append(entry.shot_key)
        return refunded


__all__ = [
    "SpeculationEntry",
    "SpeculationLedger",
    "SpeculationStatus",
]
