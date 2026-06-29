"""The alignment platform façade — the one object the rest of Kinora calls.

:class:`AlignmentService` composes the building blocks (reward model, DPO,
policy-eval guardrails, A/B harness, FT orchestrator + experiment tracking) into
two high-level workflows:

* :meth:`train_reward_model` — turn accumulated director signals into a
  calibrated reward model as a tracked fine-tuning job.
* :meth:`align_policy` — the full RLHF loop, *offline and budget-safe*: fit a gold
  reward model from pointwise signals, run a **KL sweep** of DPO policies at
  increasing temperature, evaluate each against the gold model, detect
  over-optimization, and return the **best policy that clears the KL guardrail**
  — i.e. the strongest preference-aligned policy that hasn't started reward-
  hacking. Every step is logged to the shared :class:`ExperimentTracker`.

The service is constructed with safe defaults and is fully self-contained: no
network, no DB, no live model, zero credits. It is the seam ``composition.py``
would wire a thin accessor onto (additive, lazy) when the platform is enabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .abtest import WinRateHarness
from .calibration import (
    CalibrationDiagnostics,
    IsotonicCalibrator,
    PlattCalibrator,
    reliability_curve,
)
from .dpo import DPOConfig, DPOPolicy, DPOTrainer
from .experiments import ExperimentTracker
from .linalg import Float
from .orchestrator import (
    OBJ_REWARD,
    FineTuneOrchestrator,
    FineTuneSpec,
    JobStatus,
)
from .policy import (
    GuardrailReport,
    KLGuardrail,
    OverOptimizationReport,
    PolicyEvaluator,
    PolicyReport,
    Verdict,
    over_optimization_report,
)
from .reward_model import RewardModel
from .types import PreferenceDataset, SampleDataset, as_sample_dataset


@dataclass(frozen=True)
class AlignmentConfig:
    """Tunables for the alignment workflows.

    ``kl_sweep`` is the ordered list of DPO ``beta`` temperatures to try (larger β
    pushes the policy farther per step → more KL); ``guardrail`` gates promotion.
    """

    reward_l2: float = 0.5
    dpo_lr: float = 0.3
    dpo_steps: int = 600
    dpo_l2: float = 0.1
    kl_sweep: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0)
    guardrail: KLGuardrail = field(default_factory=KLGuardrail)


@dataclass(frozen=True)
class AlignmentResult:
    """The outcome of :meth:`AlignmentService.align_policy`.

    ``best_policy`` is the chosen aligned policy (None if every candidate was
    blocked); ``chosen_beta`` is its DPO temperature; ``guardrail`` is the report
    that admitted it; ``over_optimization`` is the sweep diagnosis; ``sweep`` is the
    per-β ``(beta, kl, gold_mean, guardrail_verdict)`` trace for the metrics panel.
    """

    best_policy: DPOPolicy | None
    chosen_beta: float | None
    guardrail: GuardrailReport | None
    over_optimization: OverOptimizationReport
    sweep: tuple[tuple[float, float, float, str], ...]
    gold_model: RewardModel
    experiment: str


@dataclass
class AlignmentService:
    """High-level alignment platform; composes every facet-B component."""

    config: AlignmentConfig = field(default_factory=AlignmentConfig)
    tracker: ExperimentTracker = field(default_factory=ExperimentTracker)
    orchestrator: FineTuneOrchestrator = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.orchestrator is None:
            self.orchestrator = FineTuneOrchestrator(tracker=self.tracker)

    # -- reward model as a tracked FT job ----------------------------------- #

    def train_reward_model(
        self,
        samples: SampleDataset | object,
        *,
        name: str = "director-reward",
        l2: float | None = None,
    ) -> RewardModel:
        """Fit + track a calibrated reward model from director accept/reject signals."""

        ds = as_sample_dataset(samples)
        spec = FineTuneSpec(
            name=name,
            provider="local",
            objective=OBJ_REWARD,
            samples=ds,
            hyperparams={"l2": l2 if l2 is not None else self.config.reward_l2},
        )
        job = self.orchestrator.submit_and_run(spec, experiment=f"reward-{name}")
        if job.status is not JobStatus.SUCCEEDED or job.result is None:
            raise RuntimeError(f"reward training failed: {job.error}")
        return RewardModel.from_dict(job.result.artifact)

    def calibrate_reward_model(
        self,
        model: RewardModel,
        holdout: SampleDataset | object,
        *,
        method: str = "platt",
    ) -> tuple[PlattCalibrator | IsotonicCalibrator, CalibrationDiagnostics]:
        """Post-hoc calibrate a fitted reward model against a held-out split.

        Returns the calibrator plus a before-vs-after :class:`CalibrationDiagnostics`
        on the calibrated probabilities, so the caller can verify the recalibration
        actually lowered ECE before trusting a probability threshold.
        """

        ds = as_sample_dataset(holdout)
        feats = np.array([s.features for s in ds], dtype=Float)
        labels = np.array([1.0 if s.reward >= 0.5 else 0.0 for s in ds], dtype=Float)
        # Use the model's *logit* (unbounded score) as the calibration input.
        scores = np.array([model.logit(f) for f in feats], dtype=Float)
        if method == "platt":
            calibrator: PlattCalibrator | IsotonicCalibrator = PlattCalibrator.fit(
                scores, labels
            )
        elif method == "isotonic":
            calibrator = IsotonicCalibrator.fit(scores, labels)
        else:
            raise ValueError(f"unknown calibration method {method!r}")
        diag = reliability_curve(calibrator.transform(scores), labels)
        return calibrator, diag

    # -- the full offline RLHF loop ----------------------------------------- #

    def align_policy(
        self,
        gold_samples: SampleDataset | object,
        preferences: PreferenceDataset,
        eval_candidates: Sequence[Sequence[float]],
        *,
        name: str = "policy",
    ) -> AlignmentResult:
        """Run the KL-swept DPO alignment loop with over-optimization guarding.

        Steps: (1) fit the gold reward model from pointwise signals; (2) for each
        β in the KL sweep, fit a DPO policy against a zero reference and evaluate
        it against the gold model on ``eval_candidates``; (3) detect over-
        optimization across the sweep; (4) pick the highest-gold policy whose KL
        clears the guardrail. All runs are tracked under one experiment.
        """

        experiment = f"align-{name}"
        gold = self.train_reward_model(gold_samples, name=f"{name}-gold")
        evaluator = PolicyEvaluator(gold=gold)

        # Zero reference: the "do nothing" policy the KL is measured against.
        dim = preferences.dim
        ref = self._zero_reference(preferences, dim)

        reports: list[PolicyReport] = []
        policies: list[DPOPolicy] = []
        sweep: list[tuple[float, float, float]] = []
        for beta in self.config.kl_sweep:
            cfg = DPOConfig(
                beta=beta,
                lr=self.config.dpo_lr,
                steps=self.config.dpo_steps,
                l2=self.config.dpo_l2,
            )
            policy = DPOTrainer(cfg).fit(preferences, reference=ref)
            report = evaluator.evaluate(policy, eval_candidates, reference=ref)
            run = self.tracker.start_run(
                experiment, params={"beta": beta}, tags={"phase": "dpo-sweep"}
            )
            run.log_metric("kl", report.kl)
            run.log_metric("gold_mean", report.gold_mean)
            run.log_metric("proxy_mean", report.proxy_mean)
            run.log_metric("win_rate", report.win_rate)
            run.finish()
            reports.append(report)
            policies.append(policy)
            sweep.append((beta, report.kl, report.gold_mean))

        diag = over_optimization_report(reports)
        gold_ref_mean = float(
            np.mean(gold.reward_batch(np.atleast_2d(np.asarray(eval_candidates, dtype=Float))))
        )

        # Choose the best gold policy that clears the guardrail.
        best_idx: int | None = None
        best_report: GuardrailReport | None = None
        ordered = sorted(
            range(len(reports)), key=lambda i: reports[i].gold_mean, reverse=True
        )
        sweep_trace: list[tuple[float, float, float, str]] = []
        guardrail_verdicts: dict[int, GuardrailReport] = {}
        for i in range(len(reports)):
            gr = self.config.guardrail.check(
                kl=reports[i].kl,
                gold_policy=reports[i].gold_mean,
                gold_reference=gold_ref_mean,
            )
            guardrail_verdicts[i] = gr
        for i in ordered:
            gr = guardrail_verdicts[i]
            if gr.verdict is not Verdict.BLOCK and best_idx is None:
                best_idx = i
                best_report = gr
        for beta, kl, gm in sweep:
            idx = self.config.kl_sweep.index(beta)
            sweep_trace.append((beta, kl, gm, guardrail_verdicts[idx].verdict.value))

        return AlignmentResult(
            best_policy=policies[best_idx] if best_idx is not None else None,
            chosen_beta=self.config.kl_sweep[best_idx] if best_idx is not None else None,
            guardrail=best_report,
            over_optimization=diag,
            sweep=tuple(sweep_trace),
            gold_model=gold,
            experiment=experiment,
        )

    @staticmethod
    def _zero_reference(preferences: PreferenceDataset, dim: int) -> DPOPolicy:
        from .linalg import Standardizer

        std = Standardizer.fit(
            np.vstack(
                [[p.winner for p in preferences], [p.loser for p in preferences]]
            )
        )
        return DPOPolicy(
            theta=np.zeros(dim, dtype=Float),
            theta_ref=np.zeros(dim, dtype=Float),
            beta=0.1,
            standardizer=std,
            dim=dim,
        )

    # -- A/B convenience ---------------------------------------------------- #

    def win_rate_harness(self, gold: RewardModel, *, seed: int = 0) -> WinRateHarness:
        """A :class:`WinRateHarness` judged by ``gold`` (for offline A/B of arms)."""

        return WinRateHarness(gold=gold, seed=seed)
