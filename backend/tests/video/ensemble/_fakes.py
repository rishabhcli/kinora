"""Deterministic, network-free fakes for the best-of-N ensemble tests.

A :class:`FakeProvider` returns a scripted output (or raises a scripted error / blocks
until released); a :class:`FakeScorer` maps a provider name → a fixed
:class:`QualityScore`; a :class:`FakeBudget` is an in-memory video-seconds ledger that
records every reserve/commit/release and answers ``can_render_live`` from a flag. No
clock, no RNG, no I/O — every test is exact.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from app.video.ensemble.models import (
    BudgetReservation,
    QualityScore,
    RenderOutput,
    ShotRenderSpec,
)

_ID = itertools.count(1)


class FakeProvider:
    """A scriptable :class:`EnsembleProvider`.

    By default returns a clip tagged with its own name. ``error`` makes ``render``
    raise; ``gate`` (an :class:`asyncio.Event`) makes ``render`` block until the event
    is set (to model a slow loser the early-stop can cancel mid-flight).
    """

    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        duration_s: float = 5.0,
    ) -> None:
        self.name = name
        self._error = error
        self._gate = gate
        self._duration_s = duration_s
        self.calls = 0
        self.cancelled = False

    async def render(self, spec: ShotRenderSpec) -> RenderOutput:
        self.calls += 1
        if self._gate is not None:
            try:
                await self._gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        else:
            await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        return RenderOutput(
            model=self.name,
            duration_s=self._duration_s,
            clip_ref=f"clip://{self.name}",
            provider_task_id=f"task-{self.name}",
        )


class FakeScorer:
    """Maps provider name → a fixed :class:`QualityScore` (default composite 0.5).

    ``error_for`` names providers whose scoring should raise.
    """

    def __init__(
        self,
        scores: dict[str, QualityScore] | None = None,
        *,
        default: QualityScore | None = None,
        error_for: set[str] | None = None,
    ) -> None:
        self._scores = scores or {}
        self._default = default or QualityScore(composite=0.5)
        self._error_for = error_for or set()
        self.calls: list[str] = []

    async def score(self, output: RenderOutput, spec: ShotRenderSpec) -> QualityScore:
        await asyncio.sleep(0)
        self.calls.append(output.model)
        if output.model in self._error_for:
            raise RuntimeError(f"scorer blew up on {output.model}")
        return self._scores.get(output.model, self._default)


@dataclass
class _Ledger:
    reserved: float = 0.0
    committed: float = 0.0
    released: float = 0.0


class FakeBudget:
    """An in-memory video-seconds ledger with an explicit live gate.

    Records every operation so a test can assert that losers were released and the
    winner committed. ``ceiling`` raises on a reservation that would breach it (to
    exercise the underlying-cap propagation path).
    """

    def __init__(self, *, live: bool = True, ceiling: float | None = None) -> None:
        self._live = live
        self._ceiling = ceiling
        self.ledger = _Ledger()
        self.reserves: list[BudgetReservation] = []
        self.commits: list[str] = []
        self.releases: list[str] = []
        self._outstanding: dict[str, float] = {}

    def can_render_live(self) -> bool:
        return self._live

    async def reserve(
        self,
        video_seconds: float,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> BudgetReservation:
        await asyncio.sleep(0)
        if self._ceiling is not None and self.ledger.reserved + video_seconds > self._ceiling:
            raise RuntimeError("fake global ceiling exceeded")
        rid = f"res-{next(_ID)}"
        self.ledger.reserved += video_seconds
        self._outstanding[rid] = video_seconds
        reservation = BudgetReservation(id=rid, video_seconds=video_seconds)
        self.reserves.append(reservation)
        return reservation

    async def commit(
        self, reservation: BudgetReservation, *, actual_seconds: float | None = None
    ) -> None:
        await asyncio.sleep(0)
        seconds = reservation.video_seconds if actual_seconds is None else actual_seconds
        self.ledger.committed += seconds
        self._outstanding.pop(reservation.id, None)
        self.commits.append(reservation.id)

    async def release(self, reservation: BudgetReservation) -> None:
        await asyncio.sleep(0)
        self.ledger.released += reservation.video_seconds
        self._outstanding.pop(reservation.id, None)
        self.releases.append(reservation.id)

    @property
    def net_committed(self) -> float:
        """Seconds genuinely charged (committed − released should be 0 for losers)."""
        return self.ledger.committed

    @property
    def outstanding(self) -> dict[str, float]:
        """Reservations neither committed nor released — must be empty after a run."""
        return dict(self._outstanding)


def spec(shot_id: str = "shot-1", *, tier: str = "hero", duration_s: float = 5.0) -> ShotRenderSpec:
    """A hero-tier shot spec (the tier best-of-N is typically enabled for)."""
    return ShotRenderSpec(shot_id=shot_id, tier=tier, duration_s=duration_s, identity_key="hero-id")


@dataclass
class World:
    """A wired set of fakes for one test."""

    providers: dict[str, FakeProvider]
    scorer: FakeScorer
    budget: FakeBudget
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
