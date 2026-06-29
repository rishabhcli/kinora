"""Tests for policy evaluation, KL estimation, and over-optimization guardrails."""

from __future__ import annotations

import numpy as np
import pytest

from app.mlplatform.alignment.dpo import DPOConfig, DPOPolicy, DPOTrainer
from app.mlplatform.alignment.errors import DataError, GuardrailTripped
from app.mlplatform.alignment.linalg import Standardizer
from app.mlplatform.alignment.policy import (
    KLGuardrail,
    PolicyEvaluator,
    PolicyReport,
    Verdict,
    estimate_kl,
    over_optimization_report,
)
from app.mlplatform.alignment.reward_model import RewardModel, RewardModelTrainer
from app.mlplatform.alignment.types import (
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
)


def _gold_reward_model() -> RewardModel:
    # Gold: accept iff first feature high.
    samples = []
    rng = np.random.default_rng(0)
    for _ in range(300):
        x = float(rng.uniform(0, 1))
        y = float(rng.uniform(0, 1))
        accept = x >= 0.6
        samples.append(Sample([x, y], 1.0 if accept else 0.0))
    return RewardModelTrainer(l2=0.01).fit(SampleDataset(samples=tuple(samples)))


def _ref_policy(dim: int = 2) -> DPOPolicy:
    return DPOPolicy(
        theta=np.zeros(dim),
        theta_ref=np.zeros(dim),
        beta=0.2,
        standardizer=Standardizer(mean=np.zeros(dim), scale=np.ones(dim)),
        dim=dim,
    )


def test_estimate_kl_zero_for_identical_policy() -> None:
    ref = _ref_policy()
    cands = [[0.1, 0.2], [0.5, 0.5], [0.9, 0.1]]
    # A policy equal to its reference has zero KL.
    assert estimate_kl(ref, cands) == pytest.approx(0.0, abs=1e-9)


def test_estimate_kl_positive_when_policy_moves() -> None:
    ref = _ref_policy()
    moved = DPOPolicy(
        theta=np.array([3.0, 0.0]),
        theta_ref=np.zeros(2),
        beta=0.2,
        standardizer=ref.standardizer,
        dim=2,
    )
    cands = [[0.0, 0.0], [1.0, 0.0], [0.5, 0.5]]
    assert estimate_kl(moved, cands) > 0.0


def test_estimate_kl_rejects_empty() -> None:
    with pytest.raises(DataError):
        estimate_kl(_ref_policy(), np.empty((0, 2)))


def test_policy_evaluator_reports_gold_and_proxy() -> None:
    gold = _gold_reward_model()
    pd = PreferenceDataset(
        pairs=tuple(
            PreferencePair([0.9, 0.5], [0.2, 0.5]) for _ in range(20)
        )
    )
    policy = DPOTrainer(DPOConfig(beta=0.2, lr=0.3, steps=400)).fit(pd)
    cands = [[0.1, 0.5], [0.4, 0.5], [0.7, 0.5], [0.95, 0.5]]
    rep = PolicyEvaluator(gold=gold).evaluate(policy, cands)
    assert rep.n == 4
    assert 0.0 <= rep.win_rate <= 1.0
    # The policy that learned "prefer high x" should pick a gold-good candidate.
    assert rep.win_rate >= 0.5


def test_over_optimization_detected_when_gold_falls() -> None:
    # Synthetic sweep: proxy keeps rising; gold rises then falls (classic).
    def _r(gold: float, proxy: float, kl: float) -> PolicyReport:
        return PolicyReport(
            gold_mean=gold, proxy_mean=proxy, win_rate=0.5,
            proxy_gold_gap=proxy - gold, kl=kl, n=10,
        )

    reports = [
        _r(0.50, 0.10, 0.0),
        _r(0.70, 0.40, 0.2),
        _r(0.80, 0.70, 0.5),
        _r(0.60, 0.90, 1.0),
        _r(0.45, 0.95, 1.8),
    ]
    diag = over_optimization_report(reports)
    assert diag.over_optimized
    assert diag.best_index == 2  # gold peaked at the third step
    assert diag.best_kl == pytest.approx(0.5)
    assert diag.gold_at_end < diag.gold_peak


def test_no_over_optimization_when_gold_tracks_proxy() -> None:
    reports = [
        PolicyReport(gold_mean=0.5, proxy_mean=0.5, win_rate=0.5, proxy_gold_gap=0.0, kl=0.0, n=10),
        PolicyReport(gold_mean=0.6, proxy_mean=0.6, win_rate=0.6, proxy_gold_gap=0.0, kl=0.2, n=10),
        PolicyReport(gold_mean=0.7, proxy_mean=0.7, win_rate=0.7, proxy_gold_gap=0.0, kl=0.4, n=10),
        PolicyReport(gold_mean=0.8, proxy_mean=0.8, win_rate=0.8, proxy_gold_gap=0.0, kl=0.6, n=10),
    ]
    diag = over_optimization_report(reports)
    assert not diag.over_optimized
    assert diag.goodhart_corr > 0.9
    assert diag.best_index == 3  # gold still climbing at the end


def test_over_optimization_needs_two_points() -> None:
    with pytest.raises(DataError):
        over_optimization_report(
            [PolicyReport(0.5, 0.5, 0.5, 0.0, 0.0, 10)]
        )


def test_kl_guardrail_allow_warn_block() -> None:
    gr = KLGuardrail(kl_budget=0.5, kl_warn=0.3, min_gold_delta=0.0)
    allow = gr.check(kl=0.1, gold_policy=0.7, gold_reference=0.6)
    assert allow.verdict is Verdict.ALLOW and allow.allowed
    warn = gr.check(kl=0.4, gold_policy=0.7, gold_reference=0.6)
    assert warn.verdict is Verdict.WARN and warn.allowed
    block = gr.check(kl=0.9, gold_policy=0.7, gold_reference=0.6)
    assert block.verdict is Verdict.BLOCK and not block.allowed


def test_kl_guardrail_blocks_on_gold_regression() -> None:
    gr = KLGuardrail(kl_budget=1.0, kl_warn=0.5, min_gold_delta=0.01)
    # Within KL but the policy does not improve gold => block.
    rep = gr.check(kl=0.1, gold_policy=0.59, gold_reference=0.6)
    assert rep.verdict is Verdict.BLOCK


def test_kl_guardrail_enforce_raises() -> None:
    gr = KLGuardrail(kl_budget=0.5)
    with pytest.raises(GuardrailTripped) as exc:
        gr.enforce(kl=2.0, gold_policy=0.7, gold_reference=0.6)
    assert exc.value.report is not None


def test_kl_guardrail_validation() -> None:
    with pytest.raises(DataError):
        KLGuardrail(kl_budget=0.0)
    with pytest.raises(DataError):
        KLGuardrail(kl_budget=0.5, kl_warn=0.9)
