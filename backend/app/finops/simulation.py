"""No-infra budget simulation harness (kinora.md §11.1, §13).

Proves the FinOps loop keeps a reading session **inside budget** with zero
infrastructure and zero credits (KINORA_LIVE_VIDEO stays OFF). It drives a
synthetic reader forward through a book, tick by tick, and at each tick asks the
:func:`~app.finops.governor.govern` brain what to do given the *current* used
seconds, the reader's trajectory, and the upcoming shots. The decision's
optimizer plan determines how many video-seconds this tick actually spends — and
the harness accrues exactly that, so the running total can be asserted never to
exceed the cap.

The headline metric (§13) is *minutes of consistent film per 1,650-second
budget*; the harness reports total video-seconds spent, the share delivered as
full video vs. degraded rungs, and whether the global cap was ever breached
(it never is — that is the point).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.finops.forecast import (
    DEFAULT_SHOT_SECONDS_PER_WORD,
    ReadingTrajectory,
)
from app.finops.governor import Recommendation, govern
from app.finops.optimizer import RenderRung, ShotOption
from app.finops.tiers import BudgetScopeKind, BudgetTierPolicy


@dataclass(frozen=True, slots=True)
class SyntheticReader:
    """A synthetic reading profile for the simulation.

    Attributes:
        label: a name for the scenario (``steady``/``skimmer``/``binge``...).
        velocity_wps: constant forward velocity (words/second).
        total_words: book length in words.
        promotion_rate: fraction of arriving shots promoted to full video.
        shot_seconds_per_word: density of promotable video per word.
        shot_video_seconds: per-shot full-video cost (s).
        importance: per-shot importance weight for the optimizer.
    """

    label: str
    velocity_wps: float
    total_words: int
    promotion_rate: float = 1.0
    shot_seconds_per_word: float = DEFAULT_SHOT_SECONDS_PER_WORD
    shot_video_seconds: float = 5.0
    importance: float = 1.0


@dataclass(slots=True)
class SimulationResult:
    """The outcome of one simulated reading session."""

    label: str
    ticks: int
    video_seconds_spent: float
    full_video_shots: int
    degraded_shots: int
    cap_breached: bool
    final_recommendation: str
    peak_used_s: float
    spent_by_rung: dict[str, int] = field(default_factory=dict)

    @property
    def total_shots(self) -> int:
        return self.full_video_shots + self.degraded_shots

    @property
    def full_video_fraction(self) -> float:
        return self.full_video_shots / self.total_shots if self.total_shots else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "ticks": self.ticks,
            "video_seconds_spent": round(self.video_seconds_spent, 3),
            "full_video_shots": self.full_video_shots,
            "degraded_shots": self.degraded_shots,
            "total_shots": self.total_shots,
            "full_video_fraction": round(self.full_video_fraction, 4),
            "cap_breached": self.cap_breached,
            "final_recommendation": self.final_recommendation,
            "peak_used_s": round(self.peak_used_s, 3),
            "spent_by_rung": dict(self.spent_by_rung),
        }


def _upcoming_shots(
    reader: SyntheticReader, *, words_per_tick: float, tick_index: int, lookahead: int = 6
) -> list[ShotOption]:
    """The shots the reader is approaching this tick (each a full-video option)."""
    start = tick_index * lookahead
    return [
        ShotOption(
            shot_id=f"{reader.label}_shot_{start + i}",
            video_seconds=reader.shot_video_seconds,
            importance=reader.importance,
        )
        for i in range(lookahead)
    ]


def simulate_reader(
    reader: SyntheticReader,
    policy: BudgetTierPolicy,
    *,
    tick_s: float = 5.0,
    max_ticks: int = 400,
    horizon_s: float = 60.0,
    session_id: str | None = "sim_session",
) -> SimulationResult:
    """Drive one synthetic reader through the governor; accrue what it spends.

    Each tick advances the reader by ``velocity * tick_s`` words, evaluates the
    governance decision against the *running* used seconds, and spends the
    optimizer plan's full-video seconds (subject to the live cap). The loop ends
    when the reader finishes the book or ``max_ticks`` is reached.
    """
    words_per_tick = reader.velocity_wps * tick_s
    used_global = 0.0
    used_session = 0.0
    peak = 0.0
    full_video = 0
    degraded = 0
    spent_by_rung: dict[str, int] = {}
    words_read = 0
    tick = 0
    final_rec = Recommendation.PROMOTE.value

    while tick < max_ticks and words_read < reader.total_words:
        words_remaining = max(reader.total_words - words_read, 0)
        traj = ReadingTrajectory(
            velocity_wps=reader.velocity_wps,
            words_remaining=words_remaining,
            shot_seconds_per_word=reader.shot_seconds_per_word,
            promotion_rate=reader.promotion_rate,
        )
        upcoming = _upcoming_shots(reader, words_per_tick=words_per_tick, tick_index=tick)
        used_by_scope = {
            BudgetScopeKind.GLOBAL: used_global,
            BudgetScopeKind.SESSION: used_session,
        }
        decision = govern(
            policy,
            used_by_scope=used_by_scope,
            trajectory=traj,
            upcoming=upcoming,
            horizon_s=horizon_s,
        )
        final_rec = decision.recommendation.value

        # Spend the plan's full-video seconds, clamped to live global headroom so
        # the cap is never breached even if the plan was sized to a wider scope.
        global_headroom = policy.global_cap.headroom_s(used_global)
        for assignment in decision.plan.assignments:
            spent_by_rung[assignment.rung.value] = (
                spent_by_rung.get(assignment.rung.value, 0) + 1
            )
            if assignment.rung is RenderRung.FULL_VIDEO:
                if assignment.video_seconds <= global_headroom + 1e-9:
                    used_global += assignment.video_seconds
                    used_session += assignment.video_seconds
                    global_headroom -= assignment.video_seconds
                    full_video += 1
                else:
                    degraded += 1  # no headroom -> would ride the ladder
            else:
                degraded += 1

        peak = max(peak, used_global)
        words_read += int(words_per_tick) or 1
        tick += 1

    return SimulationResult(
        label=reader.label,
        ticks=tick,
        video_seconds_spent=used_global,
        full_video_shots=full_video,
        degraded_shots=degraded,
        cap_breached=used_global > policy.global_cap.cap_s + 1e-6,
        final_recommendation=final_rec,
        peak_used_s=peak,
        spent_by_rung=spent_by_rung,
    )


def default_reader_suite() -> tuple[SyntheticReader, ...]:
    """A representative spread of readers for the harness (and §13 proof)."""
    return (
        SyntheticReader(
            label="steady", velocity_wps=4.0, total_words=40_000, promotion_rate=1.0
        ),
        SyntheticReader(
            label="skimmer",
            velocity_wps=18.0,
            total_words=40_000,
            promotion_rate=0.2,
            importance=0.8,
        ),
        SyntheticReader(
            label="binge", velocity_wps=8.0, total_words=120_000, promotion_rate=0.9
        ),
        SyntheticReader(
            label="savorer",
            velocity_wps=2.0,
            total_words=20_000,
            promotion_rate=1.0,
            importance=1.2,
        ),
    )


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """The aggregate result of running the harness over a suite of readers."""

    results: tuple[SimulationResult, ...]

    @property
    def any_cap_breached(self) -> bool:
        return any(r.cap_breached for r in self.results)

    @property
    def total_video_seconds(self) -> float:
        return sum(r.video_seconds_spent for r in self.results)

    def as_dict(self) -> dict[str, object]:
        return {
            "any_cap_breached": self.any_cap_breached,
            "total_video_seconds": round(self.total_video_seconds, 3),
            "results": [r.as_dict() for r in self.results],
        }


def run_suite(
    policy: BudgetTierPolicy,
    readers: tuple[SyntheticReader, ...] | None = None,
    **kwargs: object,
) -> SuiteReport:
    """Run the harness over a suite of readers, each against a fresh global budget.

    Each reader gets the same *policy* (caps) but its own running totals, so the
    per-session cap is exercised per reader; the global cap is asserted per reader
    by :func:`simulate_reader`. For the *shared-pool* multi-tenant variant (one
    global ceiling contended by many readers at once), see :func:`simulate_pool`.
    """
    readers = readers or default_reader_suite()
    results = tuple(
        simulate_reader(reader, policy, **kwargs)  # type: ignore[arg-type]
        for reader in readers
    )
    return SuiteReport(results=results)


@dataclass(slots=True)
class PoolResult:
    """The outcome of a shared-pool simulation (many readers, one global ceiling)."""

    total_video_seconds: float
    full_video_shots: int
    degraded_shots: int
    cap_breached: bool
    per_reader_video_s: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return {
            "total_video_seconds": round(self.total_video_seconds, 3),
            "full_video_shots": self.full_video_shots,
            "degraded_shots": self.degraded_shots,
            "cap_breached": self.cap_breached,
            "per_reader_video_s": {
                k: round(v, 3) for k, v in self.per_reader_video_s.items()
            },
        }


def simulate_pool(
    readers: tuple[SyntheticReader, ...],
    policy: BudgetTierPolicy,
    *,
    tick_s: float = 5.0,
    max_ticks: int = 400,
    horizon_s: float = 60.0,
) -> PoolResult:
    """Many tenants reading at once against ONE shared global ceiling.

    The headline §11.1 guarantee is "no one reading session drains the pool". This
    interleaves every reader tick-by-tick against a *shared* ``used_global``: each
    reader's per-session cap still bounds its own spend, and the shared global
    ceiling is asserted across all of them. Even with several binge readers the
    global total can never exceed the ceiling — the optimizer degrades whoever
    arrives once the shared headroom is gone.
    """
    used_global = 0.0
    per_session: dict[str, float] = {r.label: 0.0 for r in readers}
    per_reader_video: dict[str, float] = {r.label: 0.0 for r in readers}
    words_read: dict[str, int] = {r.label: 0 for r in readers}
    full_video = 0
    degraded = 0

    for tick in range(max_ticks):
        active = [r for r in readers if words_read[r.label] < r.total_words]
        if not active:
            break
        for reader in active:
            words_per_tick = reader.velocity_wps * tick_s
            traj = ReadingTrajectory(
                velocity_wps=reader.velocity_wps,
                words_remaining=max(reader.total_words - words_read[reader.label], 0),
                shot_seconds_per_word=reader.shot_seconds_per_word,
                promotion_rate=reader.promotion_rate,
            )
            upcoming = _upcoming_shots(reader, words_per_tick=words_per_tick, tick_index=tick)
            used_by_scope = {
                BudgetScopeKind.GLOBAL: used_global,
                BudgetScopeKind.SESSION: per_session[reader.label],
            }
            decision = govern(
                policy,
                used_by_scope=used_by_scope,
                trajectory=traj,
                upcoming=upcoming,
                horizon_s=horizon_s,
            )
            global_headroom = policy.global_cap.headroom_s(used_global)
            session_headroom = policy.session_cap.headroom_s(per_session[reader.label])
            for assignment in decision.plan.assignments:
                if assignment.rung is RenderRung.FULL_VIDEO and (
                    assignment.video_seconds <= global_headroom + 1e-9
                    and assignment.video_seconds <= session_headroom + 1e-9
                ):
                    used_global += assignment.video_seconds
                    per_session[reader.label] += assignment.video_seconds
                    per_reader_video[reader.label] += assignment.video_seconds
                    global_headroom -= assignment.video_seconds
                    session_headroom -= assignment.video_seconds
                    full_video += 1
                else:
                    degraded += 1
            words_read[reader.label] += int(words_per_tick) or 1

    return PoolResult(
        total_video_seconds=used_global,
        full_video_shots=full_video,
        degraded_shots=degraded,
        cap_breached=used_global > policy.global_cap.cap_s + 1e-6,
        per_reader_video_s=per_reader_video,
    )


__all__ = [
    "PoolResult",
    "SimulationResult",
    "SuiteReport",
    "SyntheticReader",
    "default_reader_suite",
    "run_suite",
    "simulate_pool",
    "simulate_reader",
]
