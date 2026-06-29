"""Deterministic sim doubles for the Scheduler's collaborator protocols
(kinora.md §4.2 source-span index, §4.4 keyframes, §11.1 budget).

The :class:`~app.scheduler.service.SchedulerService` is fully constructor-injected
behind narrow ``Protocol``\\ s. The simulation passes the *real*
:class:`~app.queue.redis_queue.RedisRenderQueue` for the queue seam (so production
queue code is under test), but the budget, source-span, and keyframe seams are
modelled here because their production implementations need a DB / object store
that a virtual-time simulator must not touch.

Crucially these are **not** the existing ``app.eval.buffer_trace`` dry-run doubles.
Those exist to *prove zero spend* — ``can_render_live()`` is hard-wired ``False``,
so the scheduler never promotes and the queue→worker→render half of the loop is
never exercised. This simulator's whole point is to exercise that half end-to-end,
so :class:`SimBudget`:

* **opens the live gate** (``can_render_live() == True``) — but spends only a
  *virtual* video-second pool, never a real credit and never calling a provider,
  so ``KINORA_LIVE_VIDEO`` stays irrelevant (kinora.md gotcha respected: no
  provider call ⇒ no credit);
* models the finite **1,650-second** pool (§11.1) with a low-water **floor** so
  the sim can drive budget-exhaustion → degradation (§12.4) and assert the
  ``budget_low`` path;
* tracks live **reservations** so the cancel / lease-recovery paths (a reserved
  job that gets cancelled or orphaned) correctly *release* their earmark — the
  exact behaviour the no-double-spend invariant checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.memory.budget_service import BudgetExceeded, Reservation
from app.scheduler.model import SchedulerSession
from app.verification.simulation.core import Prng

if TYPE_CHECKING:
    # The narrow Protocol the scheduler reads shots through. ``SimShot`` satisfies
    # it structurally; annotating the source methods with the protocol type lets
    # mypy accept ``SimShotSource`` where a ``ShotSource`` is expected.
    from app.scheduler.service import SchedulerShot


@dataclass(slots=True)
class SimShot:
    """A source-span shot row (the §4.2 index entry the scheduler reads).

    Structurally satisfies the :class:`~app.scheduler.service.SchedulerShot`
    protocol — ``id``, ``beat_id``, ``scene_id``, ``source_span`` and
    ``duration_s`` are plain (settable) attributes, matching the protocol's
    mutable-attribute shape so ``SimShotSource`` is accepted as a ``ShotSource``.
    A book is a sorted list of these by ``word_index_start``; the scheduler walks
    them forward from the focus word.
    """

    id: str
    beat_id: str | None
    scene_id: str | None
    word_index_start: int
    duration_s: float | None = 5.0
    source_span: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.source_span is None:
            # The §4.2 ``source_span`` shape (word range; page elided for the sim).
            self.source_span = {
                "word_range": [self.word_index_start, self.word_index_start + 50]
            }


def build_book(
    n_shots: int,
    *,
    spacing: int = 12,
    duration_s: float = 5.0,
    shots_per_scene: int = 6,
    prefix: str = "shot",
) -> list[SimShot]:
    """Build a deterministic book of ``n_shots`` evenly-spaced shots.

    ``spacing`` words between shot starts (so a 4 wps reader takes ``spacing/4``
    seconds of reading-time per shot); ``shots_per_scene`` groups them into scenes
    for the stitch/continuation boundary. The result is the §4.2 source-span index
    the scheduler resolves scroll positions against.
    """
    shots: list[SimShot] = []
    for i in range(n_shots):
        scene = i // shots_per_scene
        shots.append(
            SimShot(
                id=f"{prefix}_{i:05d}",
                beat_id=f"beat_{i:05d}",
                scene_id=f"scene_{scene:04d}",
                word_index_start=i * spacing,
                duration_s=duration_s,
            )
        )
    return shots


class SimShotSource:
    """The §4.2 source-span index seam over an in-memory sorted book.

    Implements :class:`~app.scheduler.service.ShotSource`. ``next_uncommitted_shot``
    returns the first shot strictly after ``after_word`` that has not yet been
    handed out for this cursor advance — the scheduler tracks committed shots in
    its own buffer, so here we simply return the next shot ≥ the cursor, which is
    what the production repo does (the scheduler de-dups via its buffer + the
    queue's idempotency key).
    """

    __slots__ = ("_shots",)

    def __init__(self, shots: list[SimShot]) -> None:
        # Defensive sort: the scheduler relies on monotonic word order.
        self._shots = sorted(shots, key=lambda s: s.word_index_start)

    async def next_uncommitted_shot(self, book_id: str, after_word: int) -> SchedulerShot | None:
        for shot in self._shots:
            if shot.word_index_start >= after_word:
                return shot
        return None

    async def resolve_word_to_shot(self, book_id: str, word_index: int) -> SchedulerShot | None:
        # The shot whose span contains/precedes ``word_index`` (last start ≤ w).
        candidate: SimShot | None = None
        for shot in self._shots:
            if shot.word_index_start <= word_index:
                candidate = shot
            else:
                break
        return candidate

    @property
    def shots(self) -> list[SimShot]:
        """The sorted book (diagnostics / invariants)."""
        return self._shots


class SimBudget:
    """A finite **virtual** video-second budget with a low-water floor (§11.1).

    Implements :class:`~app.scheduler.service.BudgetGate`. The gate is *open*
    (``can_render_live`` ``True``) so the scheduler promotes and the render half of
    the loop runs — but nothing here calls a provider, so no real credit is ever
    spent. ``reserve`` debits the pool and hands back a tracked :class:`Reservation`;
    ``release`` credits it back (the cancel / orphan-recovery path); ``commit``
    makes a reservation permanent on acceptance. ``is_low`` trips below the floor,
    which is exactly when the scheduler must stop promoting full video and ride the
    degradation ladder.
    """

    __slots__ = ("_total", "_floor", "_remaining", "_reservations", "_committed", "_prng", "spent")

    def __init__(
        self,
        *,
        total_s: float = 1650.0,
        floor_s: float = 120.0,
        prng: Prng | None = None,
    ) -> None:
        self._total = total_s
        self._floor = floor_s
        self._remaining = total_s
        self._reservations: dict[str, Reservation] = {}
        self._committed = 0.0
        self._prng = prng or Prng(0xB0D)
        #: Permanently spent (committed) video-seconds — the headline §13 metric.
        self.spent = 0.0

    def can_render_live(self) -> bool:
        # Open: the sim exercises the full render path on a virtual pool. No
        # provider is invoked anywhere in the sim, so this never spends a credit.
        return True

    async def is_low(self) -> bool:
        return self._remaining <= self._floor

    def is_low_at(self, remaining: float) -> bool:
        return remaining <= self._floor

    async def remaining(self) -> float:
        return self._remaining

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        if video_seconds > self._remaining:
            # Hard cap: the scheduler treats this as "cannot afford" and stops
            # promoting (the §11.1 ceiling). Mirrors the real service's exception.
            raise BudgetExceeded(
                "ceiling",
                requested=video_seconds,
                used=self._total - self._remaining,
                cap=self._total,
            )
        rid = self._prng.hexid("rsv")
        reservation = Reservation(
            id=rid,
            video_seconds=video_seconds,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
        )
        self._reservations[rid] = reservation
        self._remaining -= video_seconds
        return reservation

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        # Idempotent: releasing an unknown / already-released reservation is a
        # no-op (a cancelled job and its reaper may both try — must not double-credit).
        held = self._reservations.pop(reservation.id, None)
        if held is not None:
            self._remaining += held.video_seconds

    def commit(self, reservation_id: str) -> float:
        """Make a reservation permanent on acceptance (debit stays, spent grows).

        Returns the committed video-seconds. Idempotent: committing twice (a
        duplicate ``clip_ready``) charges only once — the no-double-spend invariant.
        """
        held = self._reservations.pop(reservation_id, None)
        if held is None:
            return 0.0
        self._committed += held.video_seconds
        self.spent += held.video_seconds
        return held.video_seconds

    @property
    def outstanding_reservations(self) -> int:
        """Reservations neither released nor committed (must reach 0 at quiesce)."""
        return len(self._reservations)

    @property
    def outstanding_seconds(self) -> float:
        """Sum of outstanding (un-released, un-committed) reservation seconds."""
        return sum(r.video_seconds for r in self._reservations.values())

    @property
    def total(self) -> float:
        """The configured pool size."""
        return self._total

    @property
    def accounting_ok(self) -> bool:
        """``remaining + committed + outstanding == total`` (conservation law).

        This is the budget's internal ledger invariant: video-seconds are neither
        created nor destroyed, only moved between remaining / outstanding /
        committed. The eventual-consistency invariant leans on it.
        """
        ledger = self._remaining + self._committed + self.outstanding_seconds
        return abs(ledger - self._total) < 1e-6


class SimKeyframes:
    """The §4.4 keyframe lane — ensure a beat's still (zero video-seconds).

    Implements :class:`~app.scheduler.service.KeyframeMaintainer`. Keyframes are
    image-gen / canon refs in production; here we just record which beats were
    ensured so the speculative-zone behaviour is observable. Never touches the
    video budget — that is the whole point of the speculative representation.
    """

    __slots__ = ("ensured",)

    def __init__(self) -> None:
        self.ensured: list[str] = []

    async def ensure(
        self,
        session: SchedulerSession,
        *,
        book_id: str,
        beat_id: str,
        target_word: int,
        prompt: str | None = None,
    ) -> str:
        self.ensured.append(beat_id)
        return beat_id


__all__ = [
    "SimBudget",
    "SimKeyframes",
    "SimShot",
    "SimShotSource",
    "build_book",
]
