"""Multimodal QA + learned-reward subsystem for the §9.5 Critic.

This package layers a *learned, self-improving* QA system on top of — never
replacing — the four pre-registered §9.5 checks (the §13 pre-registration stays
honest: learned signals make the Critic more cautious, never silently pass a clip the
hard gate would fail). Modules:

* :mod:`app.render.reward` (sibling module) — the learned-reward core: logistic
  reward, threshold calibration, anomaly detection, pairwise A/B preference.
* :mod:`app.render.qa.dataset` — the read-only signal seam over accumulated episodic
  accept/reject outcomes + the pure adapter to labeled samples.
* :mod:`app.render.qa.calibration` — the offline pass that fits the
  :class:`CriticCalibration` bundle the Critic injects.
* :mod:`app.render.qa.identity` — per-character CCS at scale (weakest-face gate).
* :mod:`app.render.qa.temporal` — flicker / morph / extra-limb from the frame series.
* :mod:`app.render.qa.aesthetic` — perceptual / aesthetic quality proxies.
* :mod:`app.render.qa.active` — the active-learning queue (what to label next).
* :mod:`app.render.qa.metrics` — the §13 eval harness (CCS / accepted-footage
  efficiency / regen rate / style-drift variance; crew-vs-baseline comparison).
* :mod:`app.render.qa.report` — the learned-model audit (confusion / Brier / ROC-AUC
  / threshold sweep) — how an operator decides to trust the learned layer.
* :mod:`app.render.qa.drift` — fleet-level QA distribution-drift monitoring (PSI +
  windowed mean shift) for silent regressions a fixed threshold never catches.
"""

from __future__ import annotations

from app.render.qa.active import LabelCandidate, build_label_queue, score_candidate
from app.render.qa.aesthetic import AestheticReport, aesthetic_score
from app.render.qa.calibration import (
    CalibrationPass,
    CriticCalibration,
    calibrate_from_outcomes,
    calibrate_from_samples,
)
from app.render.qa.dataset import (
    QAOutcome,
    RewardSignalSource,
    build_reward_dataset,
    sample_from_qa,
)
from app.render.qa.drift import AxisDrift, DriftReport, detect_drift
from app.render.qa.identity import (
    CharacterCrops,
    CharacterIdentity,
    IdentityReport,
    verify_identities,
)
from app.render.qa.metrics import (
    ArmComparison,
    ArmMetrics,
    ShotOutcome,
    arm_metrics,
    compare_arms,
)
from app.render.qa.report import ConfusionMatrix, RewardReport, evaluate_reward
from app.render.qa.temporal import TemporalReport, temporal_coherence

__all__ = [
    "AestheticReport",
    "ArmComparison",
    "ArmMetrics",
    "AxisDrift",
    "CalibrationPass",
    "CharacterCrops",
    "CharacterIdentity",
    "ConfusionMatrix",
    "CriticCalibration",
    "DriftReport",
    "IdentityReport",
    "LabelCandidate",
    "QAOutcome",
    "RewardReport",
    "RewardSignalSource",
    "ShotOutcome",
    "TemporalReport",
    "aesthetic_score",
    "arm_metrics",
    "build_label_queue",
    "build_reward_dataset",
    "calibrate_from_outcomes",
    "calibrate_from_samples",
    "compare_arms",
    "detect_drift",
    "evaluate_reward",
    "sample_from_qa",
    "score_candidate",
    "temporal_coherence",
    "verify_identities",
]
