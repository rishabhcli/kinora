"""Learned reward + threshold calibration + anomaly + pairwise preference (§9.5, §13).

The §9.5 Critic scores four checks against pre-registered thresholds and routes the
repair from those four numbers. Those thresholds are honest priors but they are
*guesses*: the real boundary for "the director would accept this clip" is learnable
from the accept/reject signal the system already accumulates in episodic memory
(every accepted shot vs. every degraded shot) and prefs (every director edit).

This module is the **learned reward layer that sits on top of** — and never
replaces — the pre-registered gate (the §13 pre-registration must stay honest, so a
learned signal may make the Critic *more* cautious but never silently passes a clip
the pre-registered gate would fail). Everything here is **pure**: it operates on
already-measured numbers (the QA sub-scores + a director label), with deterministic,
seed-free fits (fixed init + fixed iteration count), so the learning logic is
exhaustively unit-testable with no network, no DB, and zero video-seconds.

Components
----------
* :class:`QASample` — one labeled training row (the four checks + optional extra
  perceptual axes + the director's accept/reject label).
* :func:`fit_reward` / :func:`reward_of` — ridge-regularized logistic regression that
  predicts ``P(director accepts)`` in ``[0, 1]`` from the QA features.
* :func:`calibrate_thresholds` — per-axis Youden-J boundary on the accept/reject ROC,
  clamped so a learned bound is **never looser** than the pre-registered floor.
* :func:`fit_anomaly` / :func:`score_anomaly` — robust per-axis median + MAD novelty
  detector for *novel* failure modes a fixed threshold never anticipated.
* :func:`fit_pairwise` / :func:`rank_pair` — Bradley-Terry logistic over feature
  differences for A/B "which of two candidate clips is better" learning.
* :func:`advise` — folds the above into a single :class:`RewardAdvice` value object
  that :func:`app.agents.critic.decide_qa` consumes as an *advisory* input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Feature normalization — map each raw QA axis to a 0..1 "goodness" feature
# --------------------------------------------------------------------------- #
#
# The reward model and anomaly detector work in a normalized feature space where
# 1.0 is always "ideal" and 0.0 is "worst", so a single sign convention holds for
# every axis (CCS is already 0..1; style_drift / motion_artifact are inverted; the
# timeline boolean is 1/0; aesthetic/temporal are already 0..1 goodness scores).

#: The ordered feature names — the column order every vector here uses.
FEATURE_NAMES: tuple[str, ...] = (
    "ccs",
    "style",
    "timeline",
    "motion",
    "aesthetic",
    "temporal",
)
N_FEATURES = len(FEATURE_NAMES)

#: Below this many labeled samples we do not trust a learned fit — cold start.
MIN_FIT_SAMPLES = 12
#: Below this many we keep the pre-registered thresholds (pinned calibration).
MIN_CALIBRATION_SAMPLES = 24
#: A clip the learned reward scores below this (yet that passed the gate) is flagged.
REWARD_REVIEW_FLOOR = 0.5
#: A robust-z anomaly score above this is "novel failure mode → look at it".
ANOMALY_FLAG_Z = 3.5
#: Minimum per-axis scale (in the 0..1 feature space) for the anomaly z-score, so a
#: training cloud with near-zero spread doesn't flag every tiny, benign deviation as
#: a catastrophic outlier. ~5% of the feature range is a sane noise floor.
ANOMALY_SCALE_FLOOR = 0.05


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _sigmoid(z: float) -> float:
    """Numerically-stable logistic."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QASample:
    """One labeled training row: the QA sub-scores + the director's verdict.

    ``accepted`` is the ground truth — ``True`` for a shot the director kept, and
    ``False`` for one that fell to the degradation ladder (an implicit reject) or
    was edited away. The extra ``aesthetic`` / ``temporal`` axes default to a neutral
    ``1.0`` so older records (which only have the four §9.5 checks) still slot in.
    """

    ccs: float
    style_drift: float
    timeline_ok: bool
    motion_artifact: float
    aesthetic: float = 1.0
    temporal: float = 1.0
    accepted: bool = True

    def features(self) -> tuple[float, ...]:
        """The normalized 0..1 goodness feature vector (column order = ``FEATURE_NAMES``)."""
        return (
            _clamp01(self.ccs),
            _clamp01(1.0 - self.style_drift),
            1.0 if self.timeline_ok else 0.0,
            _clamp01(1.0 - self.motion_artifact),
            _clamp01(self.aesthetic),
            _clamp01(self.temporal),
        )


@dataclass(frozen=True, slots=True)
class RewardWeights:
    """Logistic weights over the normalized features (``bias`` + one per feature).

    ``reward = sigmoid(bias + Σ wᵢ·featureᵢ)`` — the probability the director keeps a
    clip with these QA numbers. ``n_samples`` records how much data backed the fit so
    a consumer can decide whether to trust it.
    """

    bias: float = 0.0
    weights: tuple[float, ...] = (0.0,) * N_FEATURES
    n_samples: int = 0
    converged: bool = False

    def logit(self, features: Sequence[float]) -> float:
        if len(features) != N_FEATURES:
            raise ValueError(f"expected {N_FEATURES} features, got {len(features)}")
        return self.bias + math.fsum(w * f for w, f in zip(self.weights, features, strict=True))


#: A neutral prior the §9.5 four checks roughly imply: identity + timeline matter
#: most, then style and motion; aesthetic/temporal are gentle nudges. Used as the GD
#: init and as the cold-start reward when there is too little data to fit.
PRIOR_WEIGHTS = RewardWeights(
    bias=-2.0,
    weights=(2.6, 1.8, 2.2, 1.6, 0.6, 0.8),
    n_samples=0,
    converged=False,
)


@dataclass(frozen=True, slots=True)
class CalibratedThresholds:
    """Learned pass thresholds, clamped to never be looser than the §9.5 floor.

    ``pinned`` is ``True`` when there was too little data (or no separating boundary)
    and we fell back to the pre-registered floor verbatim — the honest default that
    keeps §13 pre-registration intact.
    """

    ccs_min: float = 0.85
    style_drift_max: float = 0.08
    motion_artifact_max: float = 0.25
    n_samples: int = 0
    pinned: bool = True


@dataclass(frozen=True, slots=True)
class AnomalyModel:
    """Robust per-axis location + scale (median + MAD) for novelty detection."""

    median: tuple[float, ...] = (1.0,) * N_FEATURES
    mad: tuple[float, ...] = (0.0,) * N_FEATURES
    n: int = 0


@dataclass(frozen=True, slots=True)
class RewardAdvice:
    """The learned layer's advisory for one clip — consumed by ``decide_qa``.

    The pre-registered gate still decides the verdict; this only ever *informs*:
    ``flagged_for_review`` marks a gate-passing clip whose learned reward is low or
    whose QA vector is anomalous, so the director feed can surface it for a look.
    """

    reward: float = 1.0
    anomaly: bool = False
    anomaly_score: float = 0.0
    margin: float = 1.0
    flagged_for_review: bool = False


# --------------------------------------------------------------------------- #
# Learned reward — ridge-regularized logistic regression (deterministic GD)
# --------------------------------------------------------------------------- #


def fit_reward(
    samples: Sequence[QASample],
    *,
    l2: float = 1.0,
    iters: int = 400,
    lr: float = 0.5,
    init: RewardWeights = PRIOR_WEIGHTS,
) -> RewardWeights:
    """Fit ``P(accept | QA features)`` by deterministic gradient descent.

    Returns :data:`PRIOR_WEIGHTS` unchanged when there is too little data
    (``< MIN_FIT_SAMPLES``) or when every label is identical (an undefined boundary)
    — the cold-start prior. The fit is fully deterministic: fixed init, fixed
    iteration count, no randomness, so the same data always yields the same weights.

    ``l2`` is an L2 (ridge) penalty pulling weights toward the prior — it keeps the
    fit stable on small, separable datasets (which logistic regression would
    otherwise push to infinite weights).
    """
    n = len(samples)
    if n < MIN_FIT_SAMPLES:
        return RewardWeights(init.bias, init.weights, n_samples=n, converged=False)
    labels = {s.accepted for s in samples}
    if len(labels) < 2:
        # All-accept or all-reject: no boundary to learn — keep the prior.
        return RewardWeights(init.bias, init.weights, n_samples=n, converged=False)

    feats = [s.features() for s in samples]
    ys = [1.0 if s.accepted else 0.0 for s in samples]
    bias = init.bias
    w = list(init.weights)
    prior_b, prior_w = init.bias, init.weights
    inv_n = 1.0 / n
    prev_loss = math.inf
    converged = False

    for _ in range(iters):
        grad_b = 0.0
        grad_w = [0.0] * N_FEATURES
        loss = 0.0
        for x, y in zip(feats, ys, strict=True):
            z = bias + math.fsum(wi * xi for wi, xi in zip(w, x, strict=True))
            p = _sigmoid(z)
            err = p - y
            grad_b += err
            for j in range(N_FEATURES):
                grad_w[j] += err * x[j]
            # Cross-entropy (clamped to avoid log(0)).
            pc = min(max(p, 1e-12), 1.0 - 1e-12)
            loss -= y * math.log(pc) + (1.0 - y) * math.log(1.0 - pc)
        # Ridge pull toward the prior (not toward zero — the prior encodes §9.5).
        bias -= lr * (grad_b * inv_n + l2 * inv_n * (bias - prior_b))
        for j in range(N_FEATURES):
            w[j] -= lr * (grad_w[j] * inv_n + l2 * inv_n * (w[j] - prior_w[j]))
        loss = loss * inv_n
        if abs(prev_loss - loss) < 1e-7:
            converged = True
            break
        prev_loss = loss

    return RewardWeights(round(bias, 6), tuple(round(x, 6) for x in w), n_samples=n,
                         converged=converged)


def reward_of(
    weights: RewardWeights,
    *,
    ccs: float,
    style_drift: float,
    timeline_ok: bool,
    motion_artifact: float,
    aesthetic: float = 1.0,
    temporal: float = 1.0,
) -> float:
    """The learned reward ``P(director accepts)`` in ``[0, 1]`` for one clip."""
    sample = QASample(
        ccs=ccs,
        style_drift=style_drift,
        timeline_ok=timeline_ok,
        motion_artifact=motion_artifact,
        aesthetic=aesthetic,
        temporal=temporal,
    )
    return round(_sigmoid(weights.logit(sample.features())), 6)


# --------------------------------------------------------------------------- #
# Threshold calibration — per-axis Youden-J boundary, clamped to the floor
# --------------------------------------------------------------------------- #


def _youden_threshold(
    values: Sequence[float], accepted: Sequence[bool], *, higher_is_better: bool
) -> float | None:
    """The decision boundary on one axis that maximizes Youden's J (TPR − FPR).

    Returns the candidate cut maximizing ``sensitivity + specificity − 1`` over the
    accept/reject labels, or ``None`` when one class is empty or no cut separates the
    classes at all (J ≤ 0 everywhere) — in which case the caller keeps the floor.
    """
    pos = [v for v, a in zip(values, accepted, strict=True) if a]
    neg = [v for v, a in zip(values, accepted, strict=True) if not a]
    if not pos or not neg:
        return None
    candidates = sorted(set(values))
    best_j = 0.0
    best_cut: float | None = None
    for cut in candidates:
        if higher_is_better:
            tpr = sum(1 for v in pos if v >= cut) / len(pos)
            fpr = sum(1 for v in neg if v >= cut) / len(neg)
        else:
            tpr = sum(1 for v in pos if v <= cut) / len(pos)
            fpr = sum(1 for v in neg if v <= cut) / len(neg)
        j = tpr - fpr
        if j > best_j:
            best_j = j
            best_cut = cut
    return best_cut


def calibrate_thresholds(
    samples: Sequence[QASample],
    *,
    ccs_floor: float = 0.85,
    style_drift_floor: float = 0.08,
    motion_artifact_floor: float = 0.25,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
) -> CalibratedThresholds:
    """Calibrate the three numeric thresholds from accept/reject data (§9.5, §13).

    For each axis we find the boundary that best separates accepted from rejected
    clips (Youden's J), then **clamp it to be at least as strict as the
    pre-registered floor** — a learned bound may tighten the gate but never loosen
    it, so the §13 pre-registration stays honest. With too little data, or no
    separating boundary on an axis, that axis stays pinned at the floor.
    """
    n = len(samples)
    floor = CalibratedThresholds(
        ccs_min=ccs_floor,
        style_drift_max=style_drift_floor,
        motion_artifact_max=motion_artifact_floor,
        n_samples=n,
        pinned=True,
    )
    if n < min_samples:
        return floor

    accepted = [s.accepted for s in samples]
    ccs_cut = _youden_threshold([s.ccs for s in samples], accepted, higher_is_better=True)
    style_cut = _youden_threshold(
        [s.style_drift for s in samples], accepted, higher_is_better=False
    )
    motion_cut = _youden_threshold(
        [s.motion_artifact for s in samples], accepted, higher_is_better=False
    )

    # Clamp each learned cut to be no looser than the floor.
    ccs_min = max(ccs_floor, ccs_cut) if ccs_cut is not None else ccs_floor
    style_max = min(style_drift_floor, style_cut) if style_cut is not None else style_drift_floor
    motion_max = (
        min(motion_artifact_floor, motion_cut)
        if motion_cut is not None
        else motion_artifact_floor
    )
    pinned = ccs_cut is None and style_cut is None and motion_cut is None
    return CalibratedThresholds(
        ccs_min=round(ccs_min, 4),
        style_drift_max=round(style_max, 4),
        motion_artifact_max=round(motion_max, 4),
        n_samples=n,
        pinned=pinned,
    )


# --------------------------------------------------------------------------- #
# Anomaly detection — robust per-axis median + MAD (novel failure modes)
# --------------------------------------------------------------------------- #


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def fit_anomaly(samples: Sequence[QASample]) -> AnomalyModel:
    """Fit a robust per-axis location (median) + scale (MAD) over *accepted* clips.

    We model the distribution of QA vectors the director was happy with; a future
    clip far from that cloud on any axis is a *novel* failure mode worth surfacing,
    even if it passed every fixed threshold. MAD (median absolute deviation) is used
    instead of std so a handful of degraded outliers don't inflate the scale.
    """
    good = [s.features() for s in samples if s.accepted]
    if not good:
        good = [s.features() for s in samples]
    if not good:
        return AnomalyModel()
    medians = tuple(_median([row[j] for row in good]) for j in range(N_FEATURES))
    mads = tuple(
        _median([abs(row[j] - medians[j]) for row in good]) for j in range(N_FEATURES)
    )
    return AnomalyModel(median=medians, mad=mads, n=len(good))


def score_anomaly(model: AnomalyModel, sample: QASample) -> float:
    """Max robust-z across axes (``0.6745·|x − median| / scale``) — higher = stranger.

    The ``0.6745`` constant makes MAD a consistent estimator of σ for normal data, so
    the score reads like a familiar z-score. To avoid a training cloud with near-zero
    spread flagging every tiny benign deviation, the per-axis scale is floored at
    :data:`ANOMALY_SCALE_FLOOR` (a ~5% noise floor in the 0..1 feature space) — so a
    clip is "novel" only when it is *far* from the accepted distribution.
    """
    if model.n == 0:
        return 0.0
    feats = sample.features()
    best = 0.0
    for j in range(N_FEATURES):
        scale = max(model.mad[j], ANOMALY_SCALE_FLOOR)
        diff = abs(feats[j] - model.median[j])
        z = 0.6745 * diff / scale
        best = max(best, z)
    return round(best, 4)


# --------------------------------------------------------------------------- #
# The advisory — fold reward + anomaly + margin into one value object
# --------------------------------------------------------------------------- #


def advise(
    *,
    ccs: float,
    style_drift: float,
    timeline_ok: bool,
    motion_artifact: float,
    aesthetic: float = 1.0,
    temporal: float = 1.0,
    weights: RewardWeights = PRIOR_WEIGHTS,
    anomaly_model: AnomalyModel | None = None,
    thresholds: CalibratedThresholds | None = None,
    reward_review_floor: float = REWARD_REVIEW_FLOOR,
    anomaly_flag_z: float = ANOMALY_FLAG_Z,
) -> RewardAdvice:
    """Compute the learned-layer advisory for one clip (pure).

    ``margin`` is the smallest signed distance of any numeric axis from its
    (possibly calibrated) threshold, normalized to ``[0, 1]`` — a near-zero margin
    means the clip barely passed and is a good active-learning candidate. The clip is
    ``flagged_for_review`` when the learned reward is below the floor or the QA vector
    is anomalous.
    """
    th = thresholds or CalibratedThresholds()
    reward = reward_of(
        weights,
        ccs=ccs,
        style_drift=style_drift,
        timeline_ok=timeline_ok,
        motion_artifact=motion_artifact,
        aesthetic=aesthetic,
        temporal=temporal,
    )
    sample = QASample(
        ccs=ccs,
        style_drift=style_drift,
        timeline_ok=timeline_ok,
        motion_artifact=motion_artifact,
        aesthetic=aesthetic,
        temporal=temporal,
    )
    anomaly_score = score_anomaly(anomaly_model, sample) if anomaly_model else 0.0
    anomaly = anomaly_score >= anomaly_flag_z

    # Per-axis margins (how far past the threshold, normalized) — min is the weakest.
    ccs_margin = (ccs - th.ccs_min) / max(1e-6, 1.0 - th.ccs_min) if th.ccs_min < 1.0 else 0.0
    style_margin = (th.style_drift_max - style_drift) / max(1e-6, th.style_drift_max)
    motion_margin = (th.motion_artifact_max - motion_artifact) / max(
        1e-6, th.motion_artifact_max
    )
    margin = round(min(ccs_margin, style_margin, motion_margin), 4)

    flagged = reward < reward_review_floor or anomaly
    return RewardAdvice(
        reward=reward,
        anomaly=anomaly,
        anomaly_score=anomaly_score,
        margin=margin,
        flagged_for_review=flagged,
    )


# --------------------------------------------------------------------------- #
# Pairwise A/B preference learning — Bradley-Terry over feature differences
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreferencePair:
    """A director's A/B judgment: ``winner`` was preferred over ``loser`` for a shot."""

    winner: QASample
    loser: QASample


def fit_pairwise(
    pairs: Sequence[PreferencePair],
    *,
    l2: float = 1.0,
    iters: int = 400,
    lr: float = 0.5,
    init: RewardWeights = PRIOR_WEIGHTS,
) -> RewardWeights:
    """Fit a utility ``u(x) = w·features`` so ``P(win) = sigmoid(u(winner) − u(loser))``.

    This is the Bradley-Terry model trained on feature *differences*: the bias cancels
    in a difference so only the feature weights are learned (the returned bias is
    carried through from ``init`` for use by :func:`reward_of`). Deterministic GD,
    ridge-pulled to the prior, like :func:`fit_reward`.
    """
    if len(pairs) < MIN_FIT_SAMPLES:
        return RewardWeights(init.bias, init.weights, n_samples=len(pairs), converged=False)

    diffs = [
        tuple(wf - lf for wf, lf in zip(p.winner.features(), p.loser.features(), strict=True))
        for p in pairs
    ]
    w = list(init.weights)
    prior_w = init.weights
    n = len(diffs)
    inv_n = 1.0 / n
    prev_loss = math.inf
    converged = False

    for _ in range(iters):
        grad = [0.0] * N_FEATURES
        loss = 0.0
        for d in diffs:
            z = math.fsum(wi * di for wi, di in zip(w, d, strict=True))
            p = _sigmoid(z)  # P(winner beats loser); label is always 1.
            err = p - 1.0
            for j in range(N_FEATURES):
                grad[j] += err * d[j]
            loss -= math.log(min(max(p, 1e-12), 1.0))
        for j in range(N_FEATURES):
            w[j] -= lr * (grad[j] * inv_n + l2 * inv_n * (w[j] - prior_w[j]))
        loss = loss * inv_n
        if abs(prev_loss - loss) < 1e-7:
            converged = True
            break
        prev_loss = loss

    return RewardWeights(init.bias, tuple(round(x, 6) for x in w), n_samples=n,
                         converged=converged)


def utility(weights: RewardWeights, sample: QASample) -> float:
    """The bias-free Bradley-Terry utility of a clip (higher = preferred)."""
    return math.fsum(w * f for w, f in zip(weights.weights, sample.features(), strict=True))


def rank_pair(
    weights: RewardWeights, a: QASample, b: QASample, *, tol: float = 1e-6
) -> int:
    """Rank two candidate clips: ``-1`` (a better), ``+1`` (b better), ``0`` (tie)."""
    ua, ub = utility(weights, a), utility(weights, b)
    if ua - ub > tol:
        return -1
    if ub - ua > tol:
        return 1
    return 0


def select_best(weights: RewardWeights, candidates: Sequence[QASample]) -> int:
    """Index of the highest-utility candidate (the A/B-of-N "keep the best" pick).

    This is the budget-aware "render two seeds, keep the preferred one" loop's
    decision (Phase 5, gated behind ``KINORA_LIVE_VIDEO`` for the live render). Pure:
    given the candidates' QA numbers it returns which one a director-trained
    preference model would keep. Ties break to the lowest index (deterministic).
    """
    if not candidates:
        raise ValueError("select_best requires at least one candidate")
    best_idx = 0
    best_u = utility(weights, candidates[0])
    for i in range(1, len(candidates)):
        u = utility(weights, candidates[i])
        if u > best_u:
            best_u = u
            best_idx = i
    return best_idx


# --------------------------------------------------------------------------- #
# Isotonic reward calibration — map raw logistic scores to honest probabilities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    """A monotone step function mapping a raw reward to a calibrated probability.

    A logistic reward is *ranked* well but its absolute value can be mis-calibrated
    (it says 0.8 when the empirical accept-rate at that score is 0.6). Isotonic
    regression (pool-adjacent-violators) fits the *monotone* mapping that best matches
    the observed accept-rate while preserving the ranking — so a downstream "flag if
    reward < 0.5" threshold means what it says. Stored as sorted ``(x, y)`` knots;
    :meth:`calibrate` interpolates between them and clamps outside the range.
    """

    xs: tuple[float, ...] = ()
    ys: tuple[float, ...] = ()

    def calibrate(self, raw: float) -> float:
        if not self.xs:
            return raw
        if raw <= self.xs[0]:
            return self.ys[0]
        if raw >= self.xs[-1]:
            return self.ys[-1]
        # Linear interpolation between the bracketing knots.
        lo = 0
        hi = len(self.xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.xs[mid] <= raw:
                lo = mid
            else:
                hi = mid
        x0, x1 = self.xs[lo], self.xs[hi]
        y0, y1 = self.ys[lo], self.ys[hi]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)


def fit_isotonic(points: Sequence[tuple[float, float]]) -> IsotonicCalibrator:
    """Pool-adjacent-violators isotonic fit of ``(raw_reward, accepted∈{0,1})`` pairs.

    Returns the monotone non-decreasing step function (as merged knots) that minimizes
    squared error to the labels — the standard reliability-curve calibration. Pure and
    deterministic; an empty input yields the identity calibrator.
    """
    if not points:
        return IsotonicCalibrator()
    ordered = sorted(points, key=lambda p: p[0])
    # Each block: [x_repr, sum_y, weight].
    blocks: list[list[float]] = [[x, y, 1.0] for x, y in ordered]
    i = 0
    while i < len(blocks) - 1:
        mean_i = blocks[i][1] / blocks[i][2]
        mean_next = blocks[i + 1][1] / blocks[i + 1][2]
        if mean_i <= mean_next:
            i += 1
            continue
        # Violation: pool block i with i+1, then back up to re-check monotonicity.
        blocks[i][1] += blocks[i + 1][1]
        blocks[i][2] += blocks[i + 1][2]
        del blocks[i + 1]
        if i > 0:
            i -= 1
    xs = tuple(b[0] for b in blocks)
    ys = tuple(round(b[1] / b[2], 6) for b in blocks)
    return IsotonicCalibrator(xs=xs, ys=ys)


__all__ = [
    "ANOMALY_FLAG_Z",
    "FEATURE_NAMES",
    "MIN_CALIBRATION_SAMPLES",
    "MIN_FIT_SAMPLES",
    "N_FEATURES",
    "PRIOR_WEIGHTS",
    "REWARD_REVIEW_FLOOR",
    "AnomalyModel",
    "CalibratedThresholds",
    "IsotonicCalibrator",
    "PreferencePair",
    "QASample",
    "RewardAdvice",
    "RewardWeights",
    "advise",
    "calibrate_thresholds",
    "fit_anomaly",
    "fit_isotonic",
    "fit_pairwise",
    "fit_reward",
    "rank_pair",
    "reward_of",
    "score_anomaly",
    "select_best",
    "utility",
]
