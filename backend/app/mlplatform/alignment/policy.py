"""Policy evaluation + reward-hacking / over-optimization detection (KL guardrails).

The central failure mode of RLHF is **over-optimization** (a.k.a. reward hacking):
the policy keeps climbing the *proxy* reward model while the *true* objective
(here: the director's real acceptance) plateaus or falls, because the policy has
walked into a region the reward model was never trained on and now scores
spuriously. The classic mitigation (Gao et al., 2023; the InstructGPT KL penalty)
is to **constrain the KL divergence** of the tuned policy from its reference and
to watch for the proxy/gold reward gap widening.

This module makes that measurable and enforceable, offline and deterministically:

* :class:`PolicyEvaluator` scores a candidate policy on a held-out evaluation set
  using a *gold* reward model (the trusted signal) and the policy's own *proxy*
  reward, and reports mean rewards, win-rate vs a reference, and the proxy–gold
  gap.
* :func:`estimate_kl` estimates KL(policy ‖ reference) over a candidate pool using
  the softmax distributions the implicit rewards induce — the quantity the
  guardrail caps.
* :func:`over_optimization_report` runs the diagnosis: KL within budget? proxy
  reward rising while gold reward stalls/falls (the tell-tale divergence)? a
  Goodhart correlation drop between proxy and gold across the sweep?
* :class:`KLGuardrail` turns thresholds into an allow / warn / block verdict, the
  gate the FT orchestrator consults before "promoting" a tuned policy.

Everything operates on already-fitted models and fixed candidate pools — no
sampling, no network, zero credits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .dpo import DPOPolicy
from .errors import DataError, GuardrailTripped
from .linalg import EPS, Float, FloatArray
from .reward_model import RewardModel

#: A candidate pool: a sequence of feature vectors or a 2-D ndarray. Every
#: consumer normalizes with ``np.atleast_2d(np.asarray(...))``.
Candidates = Sequence[Sequence[float]] | FloatArray


def _softmax(z: FloatArray) -> FloatArray:
    z = np.asarray(z, dtype=Float)
    z = z - np.max(z)
    e = np.exp(z)
    return e / (np.sum(e) + EPS)


def estimate_kl(
    policy: DPOPolicy,
    candidates: Candidates,
    *,
    reference: DPOPolicy | None = None,
) -> float:
    """KL(π_θ ‖ π_ref) over a discrete candidate pool.

    The log-linear policy induces a softmax distribution over the pool from its
    scores; the reference does likewise (from ``theta_ref`` if no explicit
    reference is given). Returns ``Σ_i p_i · log(p_i / q_i)`` ≥ 0, the divergence
    the over-optimization guardrail bounds.
    """

    cand = np.atleast_2d(np.asarray(candidates, dtype=Float))
    if cand.shape[0] < 1:
        raise DataError("estimate_kl needs at least one candidate")
    phi = policy.standardizer.transform(cand)
    z_pol = phi @ policy.theta
    if reference is not None:
        z_ref = policy.standardizer.transform(cand) @ reference.theta
    else:
        z_ref = phi @ policy.theta_ref
    p = _softmax(z_pol)
    q = _softmax(z_ref)
    mask = p > 0
    return float(np.sum(p[mask] * (np.log(p[mask] + EPS) - np.log(q[mask] + EPS))))


@dataclass(frozen=True)
class PolicyReport:
    """Held-out evaluation of one policy against gold + proxy reward.

    ``gold_mean`` / ``proxy_mean`` are the average rewards over the eval pool;
    ``win_rate`` is the fraction of eval candidates the policy ranks above the
    reference's choice; ``proxy_gold_gap`` = proxy_mean − gold_mean (a positive,
    growing gap is the over-optimization signature).
    """

    gold_mean: float
    proxy_mean: float
    win_rate: float
    proxy_gold_gap: float
    kl: float
    n: int


@dataclass
class PolicyEvaluator:
    """Evaluates DPO policies with a trusted *gold* reward model.

    The gold model stands in for the (expensive, ground-truth) director signal;
    the policy's own implicit reward is the *proxy*. Comparing them is how we
    detect a policy that games the proxy.
    """

    gold: RewardModel

    def evaluate(
        self,
        policy: DPOPolicy,
        candidates: Candidates,
        *,
        reference: DPOPolicy | None = None,
    ) -> PolicyReport:
        cand = np.atleast_2d(np.asarray(candidates, dtype=Float))
        if cand.shape[0] < 1:
            raise DataError("evaluate needs at least one candidate")
        gold_scores = self.gold.reward_batch(cand)
        proxy_scores = np.array([policy.implicit_reward(c) for c in cand], dtype=Float)
        # Win-rate: pick the policy-best candidate; how often does the gold model
        # agree it beats the reference-best candidate?
        if reference is not None:
            ref_scores = np.array(
                [reference.implicit_reward(c) for c in cand], dtype=Float
            )
        else:
            ref_scores = np.array(
                [c @ policy.theta_ref for c in policy.standardizer.transform(cand)],
                dtype=Float,
            )
        pol_choice = int(np.argmax(proxy_scores))
        ref_choice = int(np.argmax(ref_scores))
        win = 1.0 if gold_scores[pol_choice] > gold_scores[ref_choice] else (
            0.5 if gold_scores[pol_choice] == gold_scores[ref_choice] else 0.0
        )
        kl = estimate_kl(policy, candidates, reference=reference)
        gold_mean = float(np.mean(gold_scores))
        proxy_mean = float(np.mean(proxy_scores))
        return PolicyReport(
            gold_mean=gold_mean,
            proxy_mean=proxy_mean,
            win_rate=win,
            proxy_gold_gap=proxy_mean - gold_mean,
            kl=kl,
            n=cand.shape[0],
        )


@dataclass(frozen=True)
class OverOptimizationReport:
    """Diagnosis of over-optimization across a KL sweep of tuned policies.

    ``best_kl`` is the KL at which the *gold* reward peaked — tuning past it is
    over-optimization. ``gold_peaked`` / ``gold_at_end`` quantify how far gold has
    fallen from its peak; ``goodhart_corr`` is the proxy↔gold rank correlation
    across the sweep (a drop toward / below 0 means the proxy stopped tracking the
    truth). ``over_optimized`` is the boolean verdict.
    """

    kls: tuple[float, ...]
    gold_means: tuple[float, ...]
    proxy_means: tuple[float, ...]
    best_index: int
    best_kl: float
    gold_peak: float
    gold_at_end: float
    goodhart_corr: float
    over_optimized: bool


def over_optimization_report(
    reports: Sequence[PolicyReport],
    *,
    relative_drop: float = 0.02,
) -> OverOptimizationReport:
    """Detect over-optimization from a KL-ordered sweep of :class:`PolicyReport`.

    Given policies tuned at increasing KL from the reference, over-optimization is
    flagged when the gold reward peaks *before* the last step and then falls by
    more than ``relative_drop`` of the peak — the canonical proxy-up / gold-down
    divergence — or when the proxy↔gold correlation across the sweep is
    non-positive (Goodhart's law: the proxy has stopped tracking the truth).
    """

    if len(reports) < 2:
        raise DataError("over_optimization_report needs >= 2 swept reports")
    kls = np.array([r.kl for r in reports], dtype=Float)
    gold = np.array([r.gold_mean for r in reports], dtype=Float)
    proxy = np.array([r.proxy_mean for r in reports], dtype=Float)
    best_index = int(np.argmax(gold))
    gold_peak = float(gold[best_index])
    gold_at_end = float(gold[-1])
    peaked_early = best_index < len(reports) - 1
    fell = gold_peak - gold_at_end > relative_drop * max(abs(gold_peak), EPS)
    corr = _rank_corr(proxy, gold)
    over = (peaked_early and fell) or corr <= 0.0
    return OverOptimizationReport(
        kls=tuple(float(k) for k in kls),
        gold_means=tuple(float(g) for g in gold),
        proxy_means=tuple(float(p) for p in proxy),
        best_index=best_index,
        best_kl=float(kls[best_index]),
        gold_peak=gold_peak,
        gold_at_end=gold_at_end,
        goodhart_corr=corr,
        over_optimized=over,
    )


def _rank_corr(a: FloatArray, b: FloatArray) -> float:
    """Spearman rank correlation (Pearson on ranks); 0 if either is constant."""

    a = np.asarray(a, dtype=Float)
    b = np.asarray(b, dtype=Float)
    ra = _ranks(a)
    rb = _ranks(b)
    if np.std(ra) < EPS or np.std(rb) < EPS:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def _ranks(x: FloatArray) -> FloatArray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=Float)
    ranks[order] = np.arange(len(x), dtype=Float)
    return ranks


class Verdict(StrEnum):
    """A KL-guardrail decision."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class GuardrailReport:
    """The structured result of a :class:`KLGuardrail` check."""

    verdict: Verdict
    kl: float
    kl_budget: float
    gold_delta: float
    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.verdict is not Verdict.BLOCK


@dataclass(frozen=True)
class KLGuardrail:
    """An enforceable KL + gold-regression guardrail for policy promotion.

    * ``kl_budget`` — hard cap on KL(policy ‖ reference); exceeding it BLOCKs.
    * ``kl_warn`` — soft cap; between warn and budget yields WARN.
    * ``min_gold_delta`` — the gold reward must improve over the reference by at
      least this much, else BLOCK (a tuned policy that doesn't help is rejected).

    :meth:`check` returns a :class:`GuardrailReport`; :meth:`enforce` raises
    :class:`GuardrailTripped` on a BLOCK so the orchestrator can fail closed.
    """

    kl_budget: float = 0.5
    kl_warn: float = 0.3
    min_gold_delta: float = 0.0

    def __post_init__(self) -> None:
        if self.kl_budget <= 0:
            raise DataError("kl_budget must be > 0")
        if self.kl_warn < 0 or self.kl_warn > self.kl_budget:
            raise DataError("kl_warn must be in [0, kl_budget]")

    def check(self, *, kl: float, gold_policy: float, gold_reference: float) -> GuardrailReport:
        reasons: list[str] = []
        verdict = Verdict.ALLOW
        gold_delta = gold_policy - gold_reference
        if kl > self.kl_budget:
            verdict = Verdict.BLOCK
            reasons.append(
                f"KL {kl:.4f} exceeds budget {self.kl_budget:.4f} (over-optimization risk)"
            )
        elif kl > self.kl_warn:
            verdict = Verdict.WARN
            reasons.append(f"KL {kl:.4f} above warn threshold {self.kl_warn:.4f}")
        if gold_delta < self.min_gold_delta:
            verdict = Verdict.BLOCK
            reasons.append(
                f"gold reward delta {gold_delta:.4f} below required {self.min_gold_delta:.4f}"
            )
        if not reasons:
            reasons.append("within KL budget and improves gold reward")
        return GuardrailReport(
            verdict=verdict,
            kl=kl,
            kl_budget=self.kl_budget,
            gold_delta=gold_delta,
            reasons=tuple(reasons),
        )

    def enforce(self, *, kl: float, gold_policy: float, gold_reference: float) -> GuardrailReport:
        report = self.check(kl=kl, gold_policy=gold_policy, gold_reference=gold_reference)
        if report.verdict is Verdict.BLOCK:
            raise GuardrailTripped("; ".join(report.reasons), report=report)
        return report
