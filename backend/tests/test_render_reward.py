"""Unit tests for the learned-reward QA subsystem (§9.5, §13) — pure, no network.

Every test injects already-measured QA numbers + director accept/reject labels, so
the learning logic (logistic reward, threshold calibration, anomaly detection, and
pairwise A/B preference) is exercised deterministically with zero video-seconds.
"""

from __future__ import annotations

from app.render.reward import (
    ANOMALY_FLAG_Z,
    MIN_CALIBRATION_SAMPLES,
    MIN_FIT_SAMPLES,
    N_FEATURES,
    PRIOR_WEIGHTS,
    AnomalyModel,
    PreferencePair,
    QASample,
    advise,
    calibrate_thresholds,
    fit_anomaly,
    fit_isotonic,
    fit_pairwise,
    fit_reward,
    rank_pair,
    reward_of,
    score_anomaly,
    select_best,
    utility,
)

# --------------------------------------------------------------------------- #
# Synthetic data — a director who accepts clean clips and rejects broken ones
# --------------------------------------------------------------------------- #


def _good(n: int = 20) -> list[QASample]:
    """A run of clips a director would accept (high CCS, low drift/motion)."""
    return [
        QASample(ccs=0.92, style_drift=0.03, timeline_ok=True, motion_artifact=0.08, accepted=True)
        for _ in range(n)
    ]


def _bad(n: int = 20) -> list[QASample]:
    """A run of clips a director would reject (low CCS, high drift/motion)."""
    return [
        QASample(ccs=0.55, style_drift=0.30, timeline_ok=True, motion_artifact=0.60, accepted=False)
        for _ in range(n)
    ]


def _mixed() -> list[QASample]:
    return _good() + _bad()


# --------------------------------------------------------------------------- #
# QASample feature normalization
# --------------------------------------------------------------------------- #


def test_features_normalize_to_goodness() -> None:
    s = QASample(ccs=0.9, style_drift=0.1, timeline_ok=True, motion_artifact=0.2)
    feats = s.features()
    assert len(feats) == N_FEATURES
    assert feats[0] == 0.9  # ccs passes through
    assert abs(feats[1] - 0.9) < 1e-9  # 1 - style_drift
    assert feats[2] == 1.0  # timeline_ok true
    assert abs(feats[3] - 0.8) < 1e-9  # 1 - motion_artifact
    # aesthetic/temporal default to neutral 1.0
    assert feats[4] == 1.0 and feats[5] == 1.0


def test_features_clamp_out_of_range() -> None:
    s = QASample(ccs=1.5, style_drift=-0.2, timeline_ok=False, motion_artifact=2.0)
    feats = s.features()
    assert feats[0] == 1.0  # clamped
    assert feats[1] == 1.0  # 1 - (-0.2) clamped
    assert feats[2] == 0.0  # timeline false
    assert feats[3] == 0.0  # 1 - 2.0 clamped


# --------------------------------------------------------------------------- #
# fit_reward — learns to separate accept/reject; cold-starts safely
# --------------------------------------------------------------------------- #


def test_fit_reward_cold_start_returns_prior() -> None:
    weights = fit_reward(_good(MIN_FIT_SAMPLES - 1))
    assert weights.weights == PRIOR_WEIGHTS.weights
    assert weights.converged is False


def test_fit_reward_all_one_class_returns_prior() -> None:
    # 30 accepts, no rejects: no boundary to learn → keep the prior.
    weights = fit_reward(_good(30))
    assert weights.weights == PRIOR_WEIGHTS.weights


def test_fit_reward_separates_good_from_bad() -> None:
    weights = fit_reward(_mixed())
    good_reward = reward_of(
        weights, ccs=0.92, style_drift=0.03, timeline_ok=True, motion_artifact=0.08
    )
    bad_reward = reward_of(
        weights, ccs=0.55, style_drift=0.30, timeline_ok=True, motion_artifact=0.60
    )
    assert good_reward > 0.7
    assert bad_reward < 0.3
    assert good_reward > bad_reward


def test_fit_reward_is_deterministic() -> None:
    a = fit_reward(_mixed())
    b = fit_reward(_mixed())
    assert a == b


def test_fit_reward_converges_on_clean_data() -> None:
    weights = fit_reward(_mixed(), iters=2000)
    assert weights.converged is True


# --------------------------------------------------------------------------- #
# calibrate_thresholds — learns boundaries, never looser than the §9.5 floor
# --------------------------------------------------------------------------- #


def test_calibrate_cold_start_pins_to_floor() -> None:
    th = calibrate_thresholds(_good(MIN_CALIBRATION_SAMPLES - 1))
    assert th.pinned is True
    assert th.ccs_min == 0.85
    assert th.style_drift_max == 0.08
    assert th.motion_artifact_max == 0.25


def test_calibrate_never_loosens_below_floor() -> None:
    # A director who accepts even fairly low-CCS clips: the learned boundary would
    # want to drop below 0.85, but the floor must hold.
    samples = [
        QASample(ccs=0.70, style_drift=0.02, timeline_ok=True, motion_artifact=0.05, accepted=True)
        for _ in range(20)
    ] + [
        QASample(ccs=0.40, style_drift=0.50, timeline_ok=True, motion_artifact=0.80, accepted=False)
        for _ in range(20)
    ]
    th = calibrate_thresholds(samples)
    assert th.ccs_min >= 0.85  # clamped to the pre-registered floor
    assert th.style_drift_max <= 0.08
    assert th.motion_artifact_max <= 0.25


def test_calibrate_tightens_when_director_is_strict() -> None:
    # A strict director: accepts only very-high-CCS, very-low-drift clips, rejects
    # clips that the §9.5 floor would have passed (ccs 0.86, drift 0.07).
    strict_good = [
        QASample(ccs=0.97, style_drift=0.01, timeline_ok=True, motion_artifact=0.03, accepted=True)
        for _ in range(20)
    ]
    rejected_borderline = [
        QASample(ccs=0.86, style_drift=0.07, timeline_ok=True, motion_artifact=0.20, accepted=False)
        for _ in range(20)
    ]
    th = calibrate_thresholds(strict_good + rejected_borderline)
    # The calibrated CCS floor should tighten above 0.85 (the borderline rejects).
    assert th.ccs_min > 0.85
    assert th.pinned is False


# --------------------------------------------------------------------------- #
# Anomaly detection — flags a novel failure mode the thresholds never saw
# --------------------------------------------------------------------------- #


def test_anomaly_flags_outlier() -> None:
    model = fit_anomaly(_good(30))
    # A clip wildly off the accepted distribution on the motion axis.
    weird = QASample(ccs=0.92, style_drift=0.03, timeline_ok=True, motion_artifact=0.95)
    score = score_anomaly(model, weird)
    assert score >= ANOMALY_FLAG_Z


def test_anomaly_in_distribution_is_low() -> None:
    # Build a model with a little natural spread so MAD > 0.
    samples = [
        QASample(ccs=0.9 + 0.01 * (i % 3), style_drift=0.03 + 0.005 * (i % 4),
                 timeline_ok=True, motion_artifact=0.08 + 0.01 * (i % 3), accepted=True)
        for i in range(30)
    ]
    model = fit_anomaly(samples)
    typical = QASample(ccs=0.91, style_drift=0.035, timeline_ok=True, motion_artifact=0.085)
    assert score_anomaly(model, typical) < ANOMALY_FLAG_Z


def test_anomaly_empty_model_scores_zero() -> None:
    assert score_anomaly(AnomalyModel(), QASample(0.9, 0.03, True, 0.08)) == 0.0


# --------------------------------------------------------------------------- #
# advise — folds reward + anomaly + margin; flags low-reward / anomalous clips
# --------------------------------------------------------------------------- #


def test_advise_clean_clip_not_flagged() -> None:
    weights = fit_reward(_mixed())
    model = fit_anomaly(_good(30))
    adv = advise(
        ccs=0.93, style_drift=0.02, timeline_ok=True, motion_artifact=0.05,
        weights=weights, anomaly_model=model,
    )
    assert adv.reward > 0.7
    assert adv.flagged_for_review is False
    assert adv.anomaly is False
    assert adv.margin > 0.0


def test_advise_flags_low_reward_clip() -> None:
    # A clip that would *pass* the gate (ccs 0.86, drift 0.07, motion 0.20) but the
    # learned model scores poorly because the director rejected such clips.
    strict = [
        QASample(ccs=0.97, style_drift=0.01, timeline_ok=True, motion_artifact=0.02, accepted=True)
        for _ in range(20)
    ] + [
        QASample(ccs=0.86, style_drift=0.07, timeline_ok=True, motion_artifact=0.20, accepted=False)
        for _ in range(20)
    ]
    w2 = fit_reward(strict)
    adv = advise(
        ccs=0.86, style_drift=0.07, timeline_ok=True, motion_artifact=0.20, weights=w2,
    )
    assert adv.reward < 0.5
    assert adv.flagged_for_review is True


def test_advise_flags_anomalous_clip() -> None:
    model = fit_anomaly(_good(30))
    adv = advise(
        ccs=0.92, style_drift=0.03, timeline_ok=True, motion_artifact=0.95,
        weights=PRIOR_WEIGHTS, anomaly_model=model,
    )
    assert adv.anomaly is True
    assert adv.flagged_for_review is True


# --------------------------------------------------------------------------- #
# Pairwise A/B preference learning (Bradley-Terry)
# --------------------------------------------------------------------------- #


def test_fit_pairwise_cold_start_returns_prior() -> None:
    pairs = [
        PreferencePair(
            winner=QASample(0.95, 0.02, True, 0.05),
            loser=QASample(0.70, 0.20, True, 0.40),
        )
        for _ in range(MIN_FIT_SAMPLES - 1)
    ]
    weights = fit_pairwise(pairs)
    assert weights.weights == PRIOR_WEIGHTS.weights


def test_fit_pairwise_ranks_better_clip_higher() -> None:
    better = QASample(0.95, 0.02, True, 0.05)
    worse = QASample(0.70, 0.20, True, 0.40)
    pairs = [PreferencePair(winner=better, loser=worse) for _ in range(20)]
    weights = fit_pairwise(pairs)
    assert utility(weights, better) > utility(weights, worse)
    assert rank_pair(weights, better, worse) == -1  # a (better) wins
    assert rank_pair(weights, worse, better) == 1  # b (better) wins


def test_rank_pair_tie() -> None:
    a = QASample(0.9, 0.03, True, 0.08)
    assert rank_pair(PRIOR_WEIGHTS, a, a) == 0


def test_select_best_picks_highest_utility() -> None:
    better = QASample(0.95, 0.02, True, 0.05)
    worse = QASample(0.70, 0.20, True, 0.40)
    pairs = [PreferencePair(winner=better, loser=worse) for _ in range(20)]
    weights = fit_pairwise(pairs)
    candidates = [worse, better, QASample(0.6, 0.3, True, 0.5)]
    assert select_best(weights, candidates) == 1  # the "better" clip


def test_select_best_single_candidate() -> None:
    assert select_best(PRIOR_WEIGHTS, [QASample(0.9, 0.03, True, 0.08)]) == 0


def test_select_best_empty_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        select_best(PRIOR_WEIGHTS, [])


# --------------------------------------------------------------------------- #
# Isotonic reward calibration (reliability-curve, pool-adjacent-violators)
# --------------------------------------------------------------------------- #


def test_isotonic_empty_is_identity() -> None:
    cal = fit_isotonic([])
    assert cal.calibrate(0.7) == 0.7


def test_isotonic_is_monotone_nondecreasing() -> None:
    # Raw scores that over/under-state the true accept rate; isotonic should produce
    # a monotone mapping matching the labels.
    points = [
        (0.1, 0.0), (0.2, 0.0), (0.3, 1.0), (0.4, 0.0),  # a violation at 0.3 vs 0.4
        (0.6, 1.0), (0.7, 1.0), (0.9, 1.0),
    ]
    cal = fit_isotonic(points)
    ys = [cal.calibrate(x) for x in (0.1, 0.3, 0.5, 0.7, 0.9)]
    assert ys == sorted(ys)  # non-decreasing


def test_isotonic_pools_violation() -> None:
    # 0.3→1.0 then 0.4→0.0 violates monotonicity; PAV pools them to a mid value.
    cal = fit_isotonic([(0.3, 1.0), (0.4, 0.0)])
    assert cal.calibrate(0.3) == cal.calibrate(0.4)  # pooled
    assert abs(cal.calibrate(0.35) - 0.5) < 1e-6


def test_isotonic_clamps_outside_range() -> None:
    cal = fit_isotonic([(0.2, 0.1), (0.8, 0.9)])
    assert cal.calibrate(0.0) == 0.1  # below range → first knot
    assert cal.calibrate(1.0) == 0.9  # above range → last knot
