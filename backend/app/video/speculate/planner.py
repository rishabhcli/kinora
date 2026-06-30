"""Expected-value portfolio planner for speculative pre-generation (§4.4/§11).

Given the probability-weighted reaches from the predictor and a per-model cost
oracle, decide **which speculative shots to pre-render now** so that we *maximise
expected buffer-hit value per dollar under a hard speculative spend cap.*

The decision per candidate is the classic speculation trade-off:

    render it  ⟺  P(hit) × value  is worth  P(waste) × cost  under the budget

Concretely the planner:

1. **sizes & prices** each reach by routing it to the model class its
   hit-probability deserves (cheap turbo for long-shots, premium reserved for
   near-committed shots — :mod:`app.video.speculate.cost`), then asking the cost
   oracle for ``cost_usd`` / ``latency_s`` / ``quality``;
2. **filters** out candidates that (a) can't finish before the reader arrives
   (``latency > eta``), (b) are already cached (free — no spend needed), or (c)
   fail a minimum EV-efficiency bar (``ev_per_dollar`` below the floor) so we
   never spend on a guess whose expected value can't beat its expected waste;
3. **selects** the portfolio. Each shot key may be rendered at most once, so this
   is a 0/1 knapsack over ``cost_usd`` with value ``expected_value`` under the
   ``budget_usd`` cap. The planner uses an exact DP when the candidate set and
   budget are small (the common case — a handful of upcoming shots) and falls
   back to the greedy ``ev_per_dollar`` ratio heuristic for large inputs. Both
   are **budget-bounded by construction** — the running cost never exceeds the
   cap.

Pure: no clock, no I/O. The only economics come through the injected cost model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.video.speculate.cost import (
    RoutingPolicy,
    TieredCostModel,
    route_model_for_probability,
)
from app.video.speculate.types import (
    PredictedReach,
    SpeculationChoice,
    SpeculationPlan,
)

#: Below this expected-value-per-dollar a candidate is not worth speculating on —
#: its expected hit value can't justify its expected waste. A value of ``1.0``
#: means "expect at least a dollar of buffered value per dollar at risk".
DEFAULT_MIN_EV_PER_DOLLAR = 1.0

#: Above this many (candidates × budget-cents) the exact DP is skipped for greedy.
_EXACT_DP_CELL_BUDGET = 200_000


@dataclass(frozen=True, slots=True)
class PlannerPolicy:
    """Tunables for :class:`PortfolioPlanner` (deterministic; no env reads)."""

    #: Hard speculative spend cap for a single planning pass (dollars). The
    #: selected portfolio's total cost never exceeds this.
    budget_usd: float = 1.0
    #: Minimum EV-per-dollar a candidate must clear to be eligible.
    min_ev_per_dollar: float = DEFAULT_MIN_EV_PER_DOLLAR
    #: Cents granularity for the exact knapsack DP (finer = more exact, slower).
    cost_quantum_usd: float = 0.01
    #: Probability→model-class routing thresholds.
    routing: RoutingPolicy | None = None


class PortfolioPlanner:
    """Selects the EV-optimal, budget-bounded speculation portfolio (pure)."""

    def __init__(
        self,
        cost_model: TieredCostModel,
        policy: PlannerPolicy | None = None,
    ) -> None:
        self._cost = cost_model
        self._policy = policy or PlannerPolicy()

    @property
    def policy(self) -> PlannerPolicy:
        return self._policy

    # -- pricing ----------------------------------------------------------- #

    def price(self, reach: PredictedReach) -> SpeculationChoice:
        """Route + price a single reach into a scorable :class:`SpeculationChoice`.

        Routing first proposes the model class the hit-probability deserves
        (premium reserved for near-committed shots, cheap turbo for long-shots —
        §4.6). But a premium id is *slower*, so a high-probability shot that the
        reader will reach soon may be too close for premium to finish in time. In
        that case the pricing is **deadline-aware**: it downgrades to the highest-
        quality model that can still land before the ETA, rather than dropping the
        shot. If nothing fits the deadline, it returns the cheapest (fastest) model
        so the EV/feasibility filter in :meth:`plan` makes the final call.
        """
        seconds = reach.shot.video_seconds
        routed = route_model_for_probability(
            self._cost, reach.hit_probability, policy=self._policy.routing
        )
        model_id = self._deadline_aware_model(routed, seconds, reach.eta_s)
        return SpeculationChoice(
            reach=reach,
            model_id=model_id,
            model_class=self._cost.class_of(model_id),
            cost_usd=self._cost.cost_usd(model_id, seconds),
            quality=self._cost.quality(model_id),
            render_latency_s=self._cost.latency_s(model_id, seconds),
        )

    def _deadline_aware_model(self, routed: str, seconds: float, eta_s: float) -> str:
        """Pick a model that lands before ``eta_s``, preferring the routed one.

        Keep the routed model when it is feasible (it is the cost/quality tier the
        probability earned). Otherwise pick the best-quality model among those that
        *can* finish in time; failing that, the fastest model overall (lowest
        latency) so the shot is at least offered to the planner's filter.
        """
        if self._cost.latency_s(routed, seconds) <= max(eta_s, 0.0):
            return routed
        models = self._cost.models()
        feasible = [m for m in models if self._cost.latency_s(m, seconds) <= max(eta_s, 0.0)]
        if feasible:
            return max(feasible, key=lambda m: self._cost.quality(m))
        # Nothing fits the deadline — return the fastest so plan() can reject it
        # uniformly via the latency-exceeds-eta filter.
        return min(models, key=lambda m: self._cost.latency_s(m, seconds))

    # -- planning ---------------------------------------------------------- #

    def plan(
        self,
        reaches: Sequence[PredictedReach],
        *,
        budget_usd: float | None = None,
        already_cached: set[str] | None = None,
    ) -> SpeculationPlan:
        """Select the portfolio to launch now under the speculative budget cap.

        ``budget_usd`` overrides the policy cap (e.g. the live remaining
        speculative budget from the ledger). ``already_cached`` shot keys are
        recorded as free hits (skipped — already buffered) and never re-rendered.
        """
        cap = self._policy.budget_usd if budget_usd is None else max(0.0, budget_usd)
        cached = already_cached or set()
        plan = SpeculationPlan()

        eligible: list[SpeculationChoice] = []
        seen_keys: set[str] = set()
        for reach in reaches:
            choice = self.price(reach)
            key = choice.shot_key
            if key in cached:
                plan.skipped.append((choice, "already-cached"))
                continue
            if key in seen_keys:
                # Dedup: a shot reachable by two paths is priced once (the first,
                # highest-probability, since reaches arrive prob-sorted).
                plan.skipped.append((choice, "duplicate-shot"))
                continue
            seen_keys.add(key)
            if not choice.feasible_for(reach.eta_s):
                plan.skipped.append((choice, "latency-exceeds-eta"))
                continue
            if choice.ev_per_dollar < self._policy.min_ev_per_dollar:
                plan.skipped.append((choice, "below-ev-floor"))
                continue
            eligible.append(choice)

        selected = self._select(eligible, cap)
        chosen_keys = {c.shot_key for c in selected}
        plan.selected.extend(selected)
        for choice in eligible:
            if choice.shot_key not in chosen_keys:
                plan.skipped.append((choice, "over-budget"))
        return plan

    # -- selection (knapsack) --------------------------------------------- #

    def _select(
        self, candidates: list[SpeculationChoice], budget_usd: float
    ) -> list[SpeculationChoice]:
        """0/1 knapsack: max Σ expected_value s.t. Σ cost ≤ budget (budget-bounded)."""
        if not candidates or budget_usd <= 0.0:
            return []
        # Free candidates (zero cost) always taken — they only add value.
        free = [c for c in candidates if c.cost_usd <= 0.0]
        priced = [c for c in candidates if c.cost_usd > 0.0]

        cells = len(priced) * int(round(budget_usd / max(self._policy.cost_quantum_usd, 1e-6)))
        if cells <= _EXACT_DP_CELL_BUDGET:
            chosen = self._knapsack_exact(priced, budget_usd)
        else:
            chosen = self._knapsack_greedy(priced, budget_usd)
        return free + chosen

    def _knapsack_exact(
        self, candidates: list[SpeculationChoice], budget_usd: float
    ) -> list[SpeculationChoice]:
        """Exact 0/1 knapsack over quantised cost (small inputs only)."""
        q = max(self._policy.cost_quantum_usd, 1e-6)
        cap = int(round(budget_usd / q))
        n = len(candidates)
        weights = [max(1, int(round(c.cost_usd / q))) for c in candidates]
        # dp[w] = best (value, set-of-indices) using capacity w.
        best_value = [0.0] * (cap + 1)
        best_take: list[frozenset[int]] = [frozenset()] * (cap + 1)
        for i in range(n):
            w_i = weights[i]
            v_i = candidates[i].expected_value
            # Iterate capacities high→low so each item is used at most once.
            for w in range(cap, w_i - 1, -1):
                cand_value = best_value[w - w_i] + v_i
                if cand_value > best_value[w]:
                    best_value[w] = cand_value
                    best_take[w] = best_take[w - w_i] | {i}
        take = best_take[cap]
        return [candidates[i] for i in sorted(take)]

    def _knapsack_greedy(
        self, candidates: list[SpeculationChoice], budget_usd: float
    ) -> list[SpeculationChoice]:
        """Greedy ev_per_dollar fill (large inputs). Budget-bounded by the guard."""
        ordered = sorted(
            candidates,
            key=lambda c: (-c.ev_per_dollar, -c.expected_value, c.reach.shot.word_start),
        )
        chosen: list[SpeculationChoice] = []
        spent = 0.0
        for c in ordered:
            if spent + c.cost_usd <= budget_usd + 1e-9:
                chosen.append(c)
                spent += c.cost_usd
        return chosen


__all__ = [
    "DEFAULT_MIN_EV_PER_DOLLAR",
    "PlannerPolicy",
    "PortfolioPlanner",
]
