"""Offline A/B + win-rate harness for prompt / policy candidates (§13).

The §13 eval harness demands an *honest* comparison: pre-register the metric, run
both arms over the *same* fixed evaluation set and seeds, and report the gap with
its spread so the win isn't noise. This module is that harness for the alignment
platform — it ranks two (or many) candidate policies / prompts by **win-rate**
under a trusted judge (a gold reward model), with bootstrap confidence intervals
and a paired sign test for significance.

A *candidate* is anything that maps a fixed evaluation context to a chosen
candidate clip's feature vector — a :class:`ScoredCandidate`-emitting callable.
The two arms are scored on the *same* contexts (paired), so the comparison
controls for context difficulty exactly. All randomness (bootstrap resampling) is
seeded and deterministic; no model is called live.

Outputs:

* :class:`WinRateResult` — A's win-rate over B, ties, a paired bootstrap CI, and a
  two-sided sign-test p-value.
* :func:`tournament` — round-robin win-rates over N candidates → a ranking.

Distinct from ``llmops/ab.py`` (which A/Bs *prompt versions over a text dataset
with a heuristic judge*): this A/Bs *alignment policies / candidate selectors
over a feature-vector eval set with a gold reward model*, and adds win-rate
bootstrap CIs + a tournament ranking.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .errors import DataError
from .linalg import Float, FloatArray
from .reward_model import RewardModel

#: An arm: given one evaluation context (a feature vector), return the *score*
#: that arm assigns it (higher = the arm prefers this candidate more). The
#: harness compares two arms' scores on the same contexts under the gold judge.
Arm = Callable[[Sequence[float]], float]


@dataclass(frozen=True)
class WinRateResult:
    """The outcome of an A-vs-B win-rate comparison on a paired eval set.

    ``win_rate`` is the fraction of contexts where the *gold* judge prefers the
    candidate A picked over the one B picked (ties split 0.5). ``ci_low`` /
    ``ci_high`` bound it via a seeded paired bootstrap; ``p_value`` is the
    two-sided sign-test p-value (probability the win/loss split is chance).
    """

    name_a: str
    name_b: str
    win_rate: float
    wins: int
    losses: int
    ties: int
    n: int
    ci_low: float
    ci_high: float
    p_value: float

    @property
    def significant(self) -> bool:
        """True when the 95% CI excludes 0.5 *and* p < 0.05."""

        return (self.ci_low > 0.5 or self.ci_high < 0.5) and self.p_value < 0.05

    @property
    def winner(self) -> str:
        if self.win_rate > 0.5:
            return self.name_a
        if self.win_rate < 0.5:
            return self.name_b
        return "tie"


@dataclass
class WinRateHarness:
    """Compares candidate arms by gold-judged win-rate over a fixed eval set.

    The ``gold`` reward model is the trusted judge. ``n_bootstrap`` / ``seed``
    control the (deterministic) bootstrap CI. The eval set is a sequence of
    candidate *pairs*: for each context the harness asks each arm to score the two
    options and records which option that arm would pick, then asks the gold model
    which pick is actually better.
    """

    gold: RewardModel
    n_bootstrap: int = 1000
    seed: int = 0
    ci: float = 0.95

    def compare(
        self,
        arm_a: Arm,
        arm_b: Arm,
        eval_set: Sequence[tuple[Sequence[float], Sequence[float]]],
        *,
        name_a: str = "A",
        name_b: str = "B",
    ) -> WinRateResult:
        """A-vs-B win-rate over a paired eval set of ``(option0, option1)`` pairs."""

        if len(eval_set) == 0:
            raise DataError("eval_set must be non-empty")
        outcomes = np.empty(len(eval_set), dtype=Float)  # 1=A wins, 0=B wins, .5=tie
        wins = losses = ties = 0
        for i, (opt0, opt1) in enumerate(eval_set):
            opt0 = list(opt0)
            opt1 = list(opt1)
            a_pick = opt0 if arm_a(opt0) >= arm_a(opt1) else opt1
            b_pick = opt0 if arm_b(opt0) >= arm_b(opt1) else opt1
            g_a = self.gold.reward(a_pick)
            g_b = self.gold.reward(b_pick)
            if g_a > g_b:
                outcomes[i] = 1.0
                wins += 1
            elif g_a < g_b:
                outcomes[i] = 0.0
                losses += 1
            else:
                outcomes[i] = 0.5
                ties += 1
        win_rate = float(outcomes.mean())
        ci_low, ci_high = self._bootstrap_ci(outcomes)
        p_value = _sign_test_p(wins, losses)
        return WinRateResult(
            name_a=name_a,
            name_b=name_b,
            win_rate=win_rate,
            wins=wins,
            losses=losses,
            ties=ties,
            n=len(eval_set),
            ci_low=ci_low,
            ci_high=ci_high,
            p_value=p_value,
        )

    def _bootstrap_ci(self, outcomes: FloatArray) -> tuple[float, float]:
        rng = np.random.default_rng(self.seed)
        n = len(outcomes)
        means = np.empty(self.n_bootstrap, dtype=Float)
        for b in range(self.n_bootstrap):
            idx = rng.integers(0, n, size=n)
            means[b] = outcomes[idx].mean()
        alpha = (1.0 - self.ci) / 2.0
        lo = float(np.quantile(means, alpha))
        hi = float(np.quantile(means, 1.0 - alpha))
        return lo, hi


def _sign_test_p(wins: int, losses: int) -> float:
    """Two-sided exact binomial sign test p-value (ties dropped).

    Under H0 each non-tie outcome is a fair coin; the p-value is the probability
    of a split at least as extreme as observed. Computed exactly via the binomial
    PMF (no SciPy dependency).
    """

    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    # P(X <= k) + P(X >= n-k) under Binomial(n, 0.5), doubled-tail, capped at 1.
    log_half_n = n * np.log(0.5)
    cum = 0.0
    for i in range(0, k + 1):
        cum += np.exp(_log_comb(n, i) + log_half_n)
    p = min(1.0, 2.0 * cum)
    return float(p)


def _log_comb(n: int, k: int) -> float:
    from math import lgamma

    return lgamma(n + 1) - lgamma(k + 1) - lgamma(n - k + 1)


@dataclass(frozen=True)
class TournamentResult:
    """Round-robin ranking of N arms by average pairwise win-rate."""

    names: tuple[str, ...]
    win_matrix: FloatArray  # [i, j] = win-rate of arm i over arm j
    avg_win_rate: tuple[float, ...]
    ranking: tuple[str, ...]


def tournament(
    harness: WinRateHarness,
    arms: Mapping[str, Arm],
    eval_set: Sequence[tuple[Sequence[float], Sequence[float]]],
) -> TournamentResult:
    """Round-robin every arm against every other; rank by mean win-rate.

    Returns a :class:`TournamentResult` whose ``ranking`` lists arm names best →
    worst. The diagonal of ``win_matrix`` is 0.5 (an arm ties itself).
    """

    if len(arms) < 2:
        raise DataError("tournament needs at least 2 arms")
    names = tuple(arms.keys())
    k = len(names)
    mat = np.full((k, k), 0.5, dtype=Float)
    for i in range(k):
        for j in range(k):
            if i == j:
                continue
            res = harness.compare(
                arms[names[i]], arms[names[j]], eval_set, name_a=names[i], name_b=names[j]
            )
            mat[i, j] = res.win_rate
    # Average win-rate excludes the self-diagonal.
    avg = np.array(
        [(mat[i].sum() - mat[i, i]) / (k - 1) for i in range(k)], dtype=Float
    )
    order = np.argsort(-avg, kind="mergesort")
    ranking = tuple(names[o] for o in order)
    return TournamentResult(
        names=names,
        win_matrix=mat,
        avg_win_rate=tuple(float(a) for a in avg),
        ranking=ranking,
    )


def reward_arm(model: RewardModel) -> Arm:
    """Adapt a :class:`RewardModel` into an :data:`Arm` (scores by reward)."""

    return lambda feats: model.reward(feats)
