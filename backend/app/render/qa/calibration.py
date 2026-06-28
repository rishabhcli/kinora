"""Offline calibration pass — fit the learned-reward bundle from accumulated data.

This is the orchestration that closes the §9.5 self-improving loop: read the
accumulated accept/reject outcomes over the :class:`RewardSignalSource` seam, build
the labeled dataset, and fit the three learned artefacts the Critic consumes —

* the reward weights (``P(director accepts | QA features)``),
* the calibrated thresholds (per-axis, clamped to never loosen the §9.5 floor),
* the anomaly model (the accepted-distribution cloud novel clips are scored against).

The result is one immutable :class:`CriticCalibration` bundle. It is *injected* into
the Critic; the Critic never runs a fit inline, so the per-shot path stays cheap and
the calibration runs on a slow cadence (the idle sweeper / a periodic job, Phase 5).
Everything here is pure given the outcomes, so the whole pass is unit-testable with
an in-memory signal source and zero network.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.render.qa.dataset import QAOutcome, build_reward_dataset
from app.render.reward import (
    PRIOR_WEIGHTS,
    AnomalyModel,
    CalibratedThresholds,
    QASample,
    RewardAdvice,
    RewardWeights,
    advise,
    calibrate_thresholds,
    fit_anomaly,
    fit_reward,
)


@dataclass(frozen=True, slots=True)
class CriticCalibration:
    """The learned-reward bundle the Critic injects into its scoring path.

    A default-constructed bundle (``CriticCalibration()``) is the cold-start: prior
    weights, floor thresholds, an empty anomaly model — i.e. the Critic behaves
    exactly as today until real data has been fit. ``n_samples`` is the provenance.
    """

    weights: RewardWeights = PRIOR_WEIGHTS
    thresholds: CalibratedThresholds = field(default_factory=CalibratedThresholds)
    anomaly: AnomalyModel = field(default_factory=AnomalyModel)
    n_samples: int = 0
    book_id: str | None = None

    @property
    def is_cold_start(self) -> bool:
        """True when no real data backed the fit (the Critic runs the §9.5 defaults)."""
        return self.n_samples == 0 or (
            self.weights.n_samples == 0 and self.thresholds.pinned and self.anomaly.n == 0
        )

    def advise_clip(
        self,
        *,
        ccs: float,
        style_drift: float,
        timeline_ok: bool,
        motion_artifact: float,
        aesthetic: float = 1.0,
        temporal: float = 1.0,
    ) -> RewardAdvice:
        """Compute the learned-layer advisory for one clip from this bundle (pure)."""
        return advise(
            ccs=ccs,
            style_drift=style_drift,
            timeline_ok=timeline_ok,
            motion_artifact=motion_artifact,
            aesthetic=aesthetic,
            temporal=temporal,
            weights=self.weights,
            anomaly_model=self.anomaly,
            thresholds=self.thresholds,
        )


def calibrate_from_samples(
    samples: Sequence[QASample], *, book_id: str | None = None
) -> CriticCalibration:
    """Fit a :class:`CriticCalibration` from already-built samples (pure)."""
    weights = fit_reward(samples)
    thresholds = calibrate_thresholds(samples)
    anomaly = fit_anomaly(samples)
    return CriticCalibration(
        weights=weights,
        thresholds=thresholds,
        anomaly=anomaly,
        n_samples=len(samples),
        book_id=book_id,
    )


def calibrate_from_outcomes(
    outcomes: Sequence[QAOutcome], *, book_id: str | None = None
) -> CriticCalibration:
    """Build the dataset from stored outcomes, then fit the bundle (pure)."""
    samples = build_reward_dataset(outcomes)
    return calibrate_from_samples(samples, book_id=book_id)


class CalibrationPass:
    """Reads a :class:`RewardSignalSource` and fits a per-book calibration bundle.

    The only stateful piece (it holds the signal source); the fit itself delegates to
    the pure functions above. A Phase-5 periodic job calls :meth:`run` per book on the
    idle-sweeper cadence and stashes the bundle where the Critic can read it.
    """

    def __init__(self, source: object, *, limit: int = 500) -> None:
        self._source = source
        self._limit = limit

    async def run(self, book_id: str) -> CriticCalibration:
        """Fetch recent outcomes for ``book_id`` and fit the calibration bundle."""
        recent = getattr(self._source, "recent_outcomes", None)
        if recent is None:
            return CriticCalibration(book_id=book_id)
        outcomes = await recent(book_id, limit=self._limit)
        return calibrate_from_outcomes(outcomes, book_id=book_id)


__all__ = [
    "CalibrationPass",
    "CriticCalibration",
    "calibrate_from_outcomes",
    "calibrate_from_samples",
]
