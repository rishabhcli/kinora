"""End-to-end tests for the AlignmentService façade (the full offline RLHF loop)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from app.mlplatform.alignment import (
    AlignmentConfig,
    AlignmentService,
    KLGuardrail,
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
    Verdict,
)


def _gold_samples(n: int = 300, seed: int = 0) -> SampleDataset:
    """Director truth: accept iff ccs (feature 0) is high."""

    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        ccs = float(rng.uniform(0, 1))
        motion = float(rng.uniform(0, 1))
        rows.append(Sample([ccs, motion], 1.0 if ccs >= 0.55 else 0.0))
    return SampleDataset(samples=tuple(rows))


def _preferences(n: int = 80, seed: int = 1) -> PreferenceDataset:
    """Preferences consistent with the gold truth (prefer higher ccs)."""

    rng = np.random.default_rng(seed)
    pairs = []
    for _ in range(n):
        a = rng.uniform(0, 1, size=2)
        b = rng.uniform(0, 1, size=2)
        if abs(a[0] - b[0]) < 0.1:
            continue
        win, lose = (a, b) if a[0] > b[0] else (b, a)
        pairs.append(PreferencePair(winner=list(win), loser=list(lose)))
    return PreferenceDataset(pairs=tuple(pairs))


def _eval_pool(n: int = 12, seed: int = 2) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    return [list(rng.uniform(0, 1, size=2)) for _ in range(n)]


def _arm_first(f: Sequence[float]) -> float:
    return f[0]


def _arm_second(f: Sequence[float]) -> float:
    return f[1]


def test_train_reward_model_tracks_job() -> None:
    svc = AlignmentService()
    model = svc.train_reward_model(_gold_samples(), name="t")
    assert model.reward([0.95, 0.5]) > model.reward([0.1, 0.5])
    # The fit was tracked as a fine-tuning job + run.
    runs = svc.tracker.query(experiment="reward-t")
    assert len(runs) == 1
    assert runs[0].last_metric("accuracy") > 0.8


def test_calibrate_reward_model() -> None:
    svc = AlignmentService()
    train = _gold_samples(seed=0)
    holdout = _gold_samples(seed=99)
    model = svc.train_reward_model(train, name="cal")
    for method in ("platt", "isotonic"):
        calibrator, diag = svc.calibrate_reward_model(model, holdout, method=method)
        # Calibrated probabilities are well-formed and reasonably calibrated.
        assert 0.0 <= diag.ece <= 1.0
        assert diag.ece < 0.15
        p = calibrator.transform([model.logit([0.95, 0.5])])
        assert 0.0 <= float(p[0]) <= 1.0
    import pytest

    with pytest.raises(ValueError):
        svc.calibrate_reward_model(model, holdout, method="bogus")


def test_align_policy_returns_admissible_policy() -> None:
    svc = AlignmentService(
        config=AlignmentConfig(
            kl_sweep=(0.05, 0.1, 0.25, 0.5),
            guardrail=KLGuardrail(kl_budget=5.0, kl_warn=2.0, min_gold_delta=-1.0),
        )
    )
    result = svc.align_policy(
        _gold_samples(), _preferences(), _eval_pool(), name="demo"
    )
    # A policy was chosen and clears the (lenient) guardrail.
    assert result.best_policy is not None
    assert result.chosen_beta is not None
    assert result.guardrail is not None and result.guardrail.verdict is not Verdict.BLOCK
    # The chosen policy ranks a high-ccs candidate above a low-ccs one.
    assert result.best_policy.implicit_reward([0.9, 0.5]) > result.best_policy.implicit_reward(
        [0.1, 0.5]
    )
    # The sweep trace has one entry per beta with a guardrail verdict string.
    assert len(result.sweep) == 4
    for _beta, kl, _gold, verdict in result.sweep:
        assert kl >= 0.0
        assert verdict in {"allow", "warn", "block"}


def test_align_policy_blocks_everything_under_tight_budget() -> None:
    # An impossibly tight KL budget blocks every swept policy => no choice.
    svc = AlignmentService(
        config=AlignmentConfig(
            kl_sweep=(0.5, 1.0),
            dpo_steps=400,
            guardrail=KLGuardrail(kl_budget=1e-6, kl_warn=1e-7, min_gold_delta=0.0),
        )
    )
    result = svc.align_policy(
        _gold_samples(), _preferences(), _eval_pool(), name="tight"
    )
    assert result.best_policy is None
    assert result.chosen_beta is None
    # Every sweep entry is blocked.
    assert all(v == "block" for *_rest, v in result.sweep)


def test_align_policy_runs_are_tracked() -> None:
    svc = AlignmentService(config=AlignmentConfig(kl_sweep=(0.1, 0.5)))
    result = svc.align_policy(
        _gold_samples(), _preferences(), _eval_pool(), name="track"
    )
    sweep_runs = svc.tracker.query(experiment=result.experiment, tag=("phase", "dpo-sweep"))
    assert len(sweep_runs) == 2
    for run in sweep_runs:
        assert "kl" in run.metrics and "gold_mean" in run.metrics


def test_win_rate_harness_factory() -> None:
    svc = AlignmentService()
    gold = svc.train_reward_model(_gold_samples(), name="h")
    harness = svc.win_rate_harness(gold, seed=3)
    eval_pairs = [
        (list(p), list(q))
        for p, q in zip(_eval_pool(seed=4), _eval_pool(seed=5), strict=True)
    ]
    res = harness.compare(_arm_first, _arm_second, eval_pairs)
    assert 0.0 <= res.win_rate <= 1.0
