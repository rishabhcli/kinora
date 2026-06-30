"""The speculative pre-generation engine — orchestrator over the pure policies.

Wires the four pieces of :mod:`app.video.speculate` into one loop per reading
session:

    reader moves ──▶ predictor (probability-weighted reaches, §4.6)
                       │
                       ▼
                 planner (EV portfolio under the speculative budget cap, §4.4/§11)
                       │
                       ▼
                 ledger (reserve + launch; track started/done)
                       │
       reader's path diverges ──▶ ledger.invalidate → cancel + refund + salvage (§4.8)
                       │
                       ▼
                 accountant (record hit/waste; tune aggressiveness → next predict)

The engine is **pure policy over injectable seams**: a :class:`Clock` (virtual in
tests), a :class:`CostModelProtocol`, a :class:`CacheLookupProtocol`, and a
:class:`SpeculativeBudgetProtocol`. It awaits nothing — every step is a synchronous
decision over already-resolved state — so it is trivially deterministic. The clock
is used only to stamp launches and measure realised latency for observability; the
spend behaviour is gated entirely by the budget seam.

The engine **never spends past the speculative budget**: every launch first asks
the planner for a portfolio bounded by ``budget.remaining_usd()`` and then reserves
each choice atomically — a reservation that would breach the cap is refused and the
choice is dropped, so the ledger's ``reserved + spent`` invariant is preserved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

from app.video.speculate.accounting import SpeculationAccountant, TunerPolicy
from app.video.speculate.budget import InMemorySpeculativeBudget, NullCache
from app.video.speculate.cancellation import SpeculationLedger, SpeculationStatus
from app.video.speculate.cost import TieredCostModel
from app.video.speculate.planner import PlannerPolicy, PortfolioPlanner
from app.video.speculate.predictor import ReachModel, ReachPredictor
from app.video.speculate.protocols import (
    CacheLookupProtocol,
    CostModelProtocol,
    SpeculativeBudgetProtocol,
)
from app.video.speculate.types import (
    CancellationOutcome,
    ReaderState,
    SpeculationChoice,
    SpeculationPlan,
    UpcomingShot,
)

logger = structlog.get_logger(__name__)

#: A virtual monotonic clock returning seconds. Defaults to :func:`time.monotonic`.
ClockFn = Callable[[], float]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Static configuration for one :class:`SpeculationEngine` (no env reads)."""

    #: Hard speculative spend cap for the session (dollars).
    speculative_budget_usd: float = 1.0
    #: Forward window (in words) the new trajectory must reach for a speculation to
    #: survive an invalidation (kinora.md §4.8 keep-band).
    keep_horizon_words: int = 4000
    planner: PlannerPolicy = field(default_factory=PlannerPolicy)
    tuner: TunerPolicy = field(default_factory=TunerPolicy)
    base_reach: ReachModel = field(default_factory=ReachModel)


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """The outcome of one :meth:`SpeculationEngine.consider` pass."""

    plan: SpeculationPlan
    launched: list[SpeculationChoice] = field(default_factory=list)
    refused_budget: list[SpeculationChoice] = field(default_factory=list)

    @property
    def launched_keys(self) -> list[str]:
        return [c.shot_key for c in self.launched]


class SpeculationEngine:
    """Per-session speculative pre-generation orchestrator (pure over its seams)."""

    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        cost_model: CostModelProtocol | None = None,
        budget: SpeculativeBudgetProtocol | None = None,
        cache: CacheLookupProtocol | None = None,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._config = config or EngineConfig()
        self._cost = cost_model or TieredCostModel.default()
        self._budget = budget or InMemorySpeculativeBudget(
            self._config.speculative_budget_usd
        )
        self._cache = cache or NullCache()
        self._clock = clock

        # The cost model the planner needs is the concrete tiered one; if a caller
        # injects a bare protocol we still need the tier helpers, so require the
        # concrete type for the planner (the protocol is for the engine's own use).
        planner_cost = (
            self._cost
            if isinstance(self._cost, TieredCostModel)
            else TieredCostModel.default()
        )
        self._planner = PortfolioPlanner(planner_cost, self._config.planner)
        self._predictor = ReachPredictor(self._config.base_reach)
        self._ledger = SpeculationLedger(self._budget, self._cache)
        self._accountant = SpeculationAccountant(self._config.tuner)

    # -- introspection ----------------------------------------------------- #

    @property
    def budget(self) -> SpeculativeBudgetProtocol:
        return self._budget

    @property
    def ledger(self) -> SpeculationLedger:
        return self._ledger

    @property
    def accountant(self) -> SpeculationAccountant:
        return self._accountant

    @property
    def predictor(self) -> ReachPredictor:
        return self._predictor

    def _utilisation(self) -> float:
        if isinstance(self._budget, InMemorySpeculativeBudget):
            return self._budget.utilisation
        ceil = self._config.speculative_budget_usd
        if ceil <= 1e-9:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self._budget.remaining_usd() / ceil))

    # -- the loop ---------------------------------------------------------- #

    def consider(
        self,
        state: ReaderState,
        upcoming: Sequence[UpcomingShot],
    ) -> LaunchResult:
        """Predict, plan under the budget cap, and launch the chosen speculations.

        Returns the plan plus what was actually launched (reservations succeeded)
        vs. refused (a late reservation that no longer fit the cap — defensive; the
        plan is already budget-bounded, so refusals are rare race guards). The
        predictor uses the *current* tuned aggressiveness from the accountant.
        """
        # 1) Re-tune the predictor from the realised hit/waste record (§4.6).
        factor = self._accountant.aggressiveness(
            budget_utilisation=self._utilisation()
        )
        tuned = self._config.base_reach.with_aggressiveness(factor)
        self._predictor = ReachPredictor(tuned)

        # 2) Predict probability-weighted reaches.
        reaches = self._predictor.predict(state, upcoming)

        # 3) Plan the EV-optimal portfolio under the live remaining budget. Skip
        #    shots already cached (free hits) and shots we already launched.
        cached = {
            s.shot_key
            for s in upcoming
            if self._cache.has(s.shot_key)
            or (
                (e := self._ledger.entry(s.shot_key)) is not None
                and e.status is not SpeculationStatus.CANCELLED
            )
        }
        plan = self._planner.plan(
            reaches,
            budget_usd=self._budget.remaining_usd(),
            already_cached=cached,
        )

        # 4) Reserve + launch. Each reservation is atomic against the cap.
        result = LaunchResult(plan=plan)
        for choice in plan.selected:
            if self._budget.reserve(choice.cost_usd):
                self._ledger.register(choice)
                self._accountant.record_launch(choice.cost_usd)
                result.launched.append(choice)
            else:
                result.refused_budget.append(choice)

        logger.debug(
            "speculate.consider",
            focus_word=state.focus_word,
            reaches=len(reaches),
            selected=len(plan.selected),
            launched=len(result.launched),
            aggressiveness=round(factor, 4),
            remaining_usd=round(self._budget.remaining_usd(), 6),
        )
        return result

    # -- lifecycle hooks --------------------------------------------------- #

    def mark_started(self, shot_key: str) -> None:
        """A launched speculation's render began (its reservation becomes spend)."""
        self._ledger.mark_running(shot_key)

    def mark_landed(self, shot_key: str) -> None:
        """A launched speculation's asset landed in the cache."""
        self._ledger.mark_done(shot_key)

    def on_reader_arrived(self, shot_key: str) -> None:
        """The reader actually reached ``shot_key`` — record the hit (the bet won)."""
        entry = self._ledger.entry(shot_key)
        if entry is None:
            return
        value = entry.video_seconds  # value defaults to duration; engine has no shot copy
        self._accountant.record_hit(cost_usd=entry.reserved_usd, value=value)
        logger.debug("speculate.hit", shot_key=shot_key, value=round(value, 4))

    def on_path_diverged(self, new_focus_word: int) -> CancellationOutcome:
        """The reader's path changed — cancel unreachable speculations (§4.8).

        Cancels speculations outside the forward keep-band from ``new_focus_word``,
        refunding unstarted reservations and salvaging cacheable assets, then
        records each cancellation as a (partially-refunded) waste so the tuner
        learns to pull in. Returns the :class:`CancellationOutcome`.
        """
        # Snapshot the to-be-cancelled entries' costs *before* invalidation mutates
        # them, so the accountant sees the right sunk/refund split.
        pre = {e.shot_key: e.reserved_usd for e in self._ledger.active}
        outcome = self._ledger.invalidate(
            new_focus_word=new_focus_word,
            keep_horizon_words=self._config.keep_horizon_words,
        )
        # Each cancelled shot was either fully refunded (unstarted) or sunk.
        refunded_keys = set()
        # invalidate refunds only PENDING entries; reconstruct per-key refund.
        # An entry now CANCELLED+released was refunded its full reserved cost.
        for key in outcome.cancelled:
            entry = self._ledger.entry(key)
            cost = pre.get(key, 0.0)
            refund = cost if (entry is not None and entry.released) else 0.0
            if refund > 0.0:
                refunded_keys.add(key)
            self._accountant.record_waste(cost_usd=cost, refunded_usd=refund)
        logger.debug(
            "speculate.diverged",
            new_focus_word=new_focus_word,
            cancelled=len(outcome.cancelled),
            kept=len(outcome.kept),
            refunded_usd=outcome.refunded_usd,
            salvaged=len(outcome.salvaged),
        )
        return outcome


__all__ = [
    "ClockFn",
    "EngineConfig",
    "LaunchResult",
    "SpeculationEngine",
]
