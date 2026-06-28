"""Active-learning queue — pick the clips worth a human label next (§9.5 self-improve).

The learned reward improves only as fast as it gets labels. Most accepted clips are
uninformative (the model is already confident and right); the *informative* ones are
the borderline passes (the model is unsure — small margin) and the anomalies (the
model has never seen anything like them). This module ranks gate-passing clips by how
much a human accept/reject label would teach, so a thumbs-up/down UI (Phase 5) can ask
about the few clips that matter instead of everything.

This is the classic active-learning trifecta, made deterministic:

* **uncertainty** — clips whose learned reward is near 0.5 (the decision boundary);
* **margin** — clips that barely cleared a (possibly calibrated) threshold;
* **novelty** — clips flagged anomalous by the out-of-distribution detector.

It is pure over already-computed :class:`~app.render.reward.RewardAdvice` values, so
no model call and no I/O; the queue is just a scored, capped, deduped priority list.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.render.reward import ANOMALY_FLAG_Z, RewardAdvice

#: Blend weights for the three informativeness signals (sum to 1.0).
_W_UNCERTAINTY, _W_MARGIN, _W_NOVELTY = 0.4, 0.3, 0.3


@dataclass(frozen=True, slots=True)
class LabelCandidate:
    """One clip queued for a human label, with *why* it is informative."""

    shot_id: str
    priority: float
    uncertainty: float
    margin: float
    novelty: float
    reward: float
    anomaly: bool
    reason: str


def _uncertainty(reward: float) -> float:
    """Boundary uncertainty: 1.0 at reward 0.5, 0.0 at a confident 0/1."""
    return round(1.0 - 2.0 * abs(reward - 0.5), 4)


def _margin_informativeness(margin: float) -> float:
    """A small (near-zero) pass margin is the most informative; a big margin isn't.

    ``margin`` is the §reward advisory's signed distance of the weakest axis from its
    threshold; clamped to [0,1] and inverted so a barely-passing clip scores high.
    """
    clamped = max(0.0, min(1.0, margin))
    return round(1.0 - clamped, 4)


def _novelty(anomaly_score: float, *, flag_z: float = ANOMALY_FLAG_Z) -> float:
    """Anomaly score normalized to 0..1 against the flag threshold (capped at 2×)."""
    if flag_z <= 0:
        return 0.0
    return round(min(1.0, anomaly_score / (2.0 * flag_z)), 4)


def score_candidate(shot_id: str, advice: RewardAdvice) -> LabelCandidate:
    """Score one clip's informativeness from its :class:`RewardAdvice` (pure)."""
    unc = _uncertainty(advice.reward)
    mar = _margin_informativeness(advice.margin)
    nov = _novelty(advice.anomaly_score)
    priority = round(_W_UNCERTAINTY * unc + _W_MARGIN * mar + _W_NOVELTY * nov, 4)
    reasons = []
    if advice.anomaly:
        reasons.append("novel failure mode")
    if unc > 0.5:
        reasons.append("model uncertain")
    if mar > 0.7:
        reasons.append("barely passed gate")
    reason = "; ".join(reasons) or "low information"
    return LabelCandidate(
        shot_id=shot_id,
        priority=priority,
        uncertainty=unc,
        margin=mar,
        novelty=nov,
        reward=advice.reward,
        anomaly=advice.anomaly,
        reason=reason,
    )


def build_label_queue(
    advised: Iterable[tuple[str, RewardAdvice]],
    *,
    limit: int = 20,
    min_priority: float = 0.2,
) -> list[LabelCandidate]:
    """Rank gate-passing clips by how much a human label would teach (pure).

    ``advised`` is ``(shot_id, advice)`` pairs. Returns the top ``limit`` candidates
    above ``min_priority``, highest-priority first, deduped by ``shot_id`` (keeping
    the highest-priority occurrence) — a stable, deterministic queue.
    """
    by_shot: dict[str, LabelCandidate] = {}
    for shot_id, advice in advised:
        cand = score_candidate(shot_id, advice)
        if cand.priority < min_priority:
            continue
        existing = by_shot.get(shot_id)
        if existing is None or cand.priority > existing.priority:
            by_shot[shot_id] = cand
    ranked = sorted(by_shot.values(), key=lambda c: (-c.priority, c.shot_id))
    return ranked[:limit]


__all__ = [
    "LabelCandidate",
    "build_label_queue",
    "score_candidate",
]
