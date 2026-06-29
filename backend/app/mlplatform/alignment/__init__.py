"""Kinora alignment / preference-optimization platform (``app.mlplatform.alignment``).

A self-contained, offline-by-construction RLHF stack layered over the six-agent
crew and the §9.5 Critic (see ``DESIGN.md`` for the module map and the design
rationale). It learns *what the director actually wants* from the accept / reject /
edit signals the system already logs, then optimizes prompt / policy candidates
toward that — with KL guardrails that stop the optimization from reward-hacking.

Public surface (everything below is import-stable):

* **Data contract** — :class:`Sample`, :class:`PreferencePair`,
  :class:`SampleDataset`, :class:`PreferenceDataset`, :func:`as_sample_dataset`
  (the seam that consumes facet A's ``Dataset``).
* **Reward model** — :class:`RewardModel`, :class:`RewardModelTrainer`,
  :class:`RewardMetrics`.
* **Preference optimization** — :class:`DPOTrainer`, :class:`DPOPolicy`,
  :class:`DPOConfig`.
* **Policy evaluation + guardrails** — :class:`PolicyEvaluator`,
  :class:`KLGuardrail`, :func:`estimate_kl`, :func:`over_optimization_report`.
* **Offline A/B + win-rate** — :class:`WinRateHarness`, :func:`tournament`.
* **Orchestration + tracking** — :class:`FineTuneOrchestrator`,
  :class:`FineTuneSpec`, :class:`ExperimentTracker`.
* **Façade** — :class:`AlignmentService` (the one object the rest of Kinora calls).
"""

from __future__ import annotations

from .abtest import (
    Arm,
    TournamentResult,
    WinRateHarness,
    WinRateResult,
    reward_arm,
    tournament,
)
from .acquisition import (
    AcquisitionConfig,
    PairQuery,
    labeling_priority,
    select_pairs,
)
from .calibration import (
    CalibrationDiagnostics,
    IsotonicCalibrator,
    PlattCalibrator,
    reliability_curve,
)
from .dpo import (
    DPOConfig,
    DPOPolicy,
    DPOTrainer,
    dpo_loss,
    preference_accuracy,
)
from .errors import (
    AlignmentError,
    ConvergenceError,
    DataError,
    ExperimentError,
    GuardrailTripped,
    NotFittedError,
    OrchestrationError,
)
from .experiments import Experiment, ExperimentTracker, Run
from .orchestrator import (
    OBJECTIVES,
    FineTuneExecutor,
    FineTuneJob,
    FineTuneOrchestrator,
    FineTuneResult,
    FineTuneSpec,
    JobStatus,
    LocalExecutor,
)
from .policy import (
    GuardrailReport,
    KLGuardrail,
    OverOptimizationReport,
    PolicyEvaluator,
    PolicyReport,
    Verdict,
    estimate_kl,
    over_optimization_report,
)
from .reward_model import RewardMetrics, RewardModel, RewardModelTrainer
from .service import AlignmentConfig, AlignmentResult, AlignmentService
from .signals import (
    DirectorEvent,
    build_sample_dataset,
    pairs_from_events,
    sample_from_event,
)
from .types import (
    ACCEPT,
    DEGRADE,
    EDIT,
    REJECT,
    DatasetLike,
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
    as_sample_dataset,
)

__all__ = [
    # errors
    "AlignmentError",
    "DataError",
    "NotFittedError",
    "ConvergenceError",
    "GuardrailTripped",
    "OrchestrationError",
    "ExperimentError",
    # data contract
    "Sample",
    "PreferencePair",
    "SampleDataset",
    "PreferenceDataset",
    "DatasetLike",
    "as_sample_dataset",
    "ACCEPT",
    "REJECT",
    "EDIT",
    "DEGRADE",
    # reward model
    "RewardModel",
    "RewardModelTrainer",
    "RewardMetrics",
    # dpo
    "DPOTrainer",
    "DPOPolicy",
    "DPOConfig",
    "dpo_loss",
    "preference_accuracy",
    # policy eval + guardrails
    "PolicyEvaluator",
    "PolicyReport",
    "KLGuardrail",
    "GuardrailReport",
    "Verdict",
    "OverOptimizationReport",
    "estimate_kl",
    "over_optimization_report",
    # a/b
    "WinRateHarness",
    "WinRateResult",
    "TournamentResult",
    "tournament",
    "reward_arm",
    "Arm",
    # orchestration + tracking
    "FineTuneOrchestrator",
    "FineTuneJob",
    "FineTuneSpec",
    "FineTuneResult",
    "FineTuneExecutor",
    "LocalExecutor",
    "JobStatus",
    "OBJECTIVES",
    "ExperimentTracker",
    "Experiment",
    "Run",
    # calibration
    "PlattCalibrator",
    "IsotonicCalibrator",
    "CalibrationDiagnostics",
    "reliability_curve",
    # signal ingestion
    "DirectorEvent",
    "sample_from_event",
    "build_sample_dataset",
    "pairs_from_events",
    # active learning
    "select_pairs",
    "labeling_priority",
    "PairQuery",
    "AcquisitionConfig",
    # façade
    "AlignmentService",
    "AlignmentConfig",
    "AlignmentResult",
]
