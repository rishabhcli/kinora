"""Active-learning queue — rank gate-passing clips by how much a label would teach."""

from __future__ import annotations

from app.render.qa.active import build_label_queue, score_candidate
from app.render.reward import RewardAdvice


def test_uncertain_clip_scores_high() -> None:
    # Reward near 0.5 = on the decision boundary = informative.
    cand = score_candidate("s1", RewardAdvice(reward=0.5, margin=0.5, anomaly_score=0.0))
    assert cand.uncertainty == 1.0
    assert "uncertain" in cand.reason


def test_confident_clip_scores_low() -> None:
    cand = score_candidate("s1", RewardAdvice(reward=0.98, margin=0.9, anomaly_score=0.0))
    assert cand.uncertainty < 0.1
    assert cand.priority < 0.3


def test_barely_passing_clip_is_informative() -> None:
    cand = score_candidate("s1", RewardAdvice(reward=0.7, margin=0.01, anomaly_score=0.0))
    assert cand.margin > 0.9  # tiny pass margin → high margin-informativeness
    assert "barely passed" in cand.reason


def test_anomaly_drives_novelty() -> None:
    cand = score_candidate(
        "s1", RewardAdvice(reward=0.8, margin=0.5, anomaly=True, anomaly_score=7.0)
    )
    assert cand.novelty > 0.5
    assert "novel failure mode" in cand.reason


def test_queue_ranks_and_caps() -> None:
    advised = [
        ("confident", RewardAdvice(reward=0.99, margin=0.9, anomaly_score=0.0)),
        ("uncertain", RewardAdvice(reward=0.5, margin=0.5, anomaly_score=0.0)),
        ("anomalous", RewardAdvice(reward=0.8, margin=0.4, anomaly=True, anomaly_score=8.0)),
    ]
    queue = build_label_queue(advised, limit=2, min_priority=0.2)
    assert len(queue) == 2  # capped
    ids = [c.shot_id for c in queue]
    assert "confident" not in ids  # below min_priority, filtered out
    # highest priority first
    assert queue[0].priority >= queue[1].priority


def test_queue_dedups_by_shot_keeping_highest() -> None:
    advised = [
        ("s1", RewardAdvice(reward=0.9, margin=0.8, anomaly_score=0.0)),
        ("s1", RewardAdvice(reward=0.5, margin=0.1, anomaly_score=0.0)),  # more informative
    ]
    queue = build_label_queue(advised, limit=10, min_priority=0.0)
    assert len(queue) == 1
    assert queue[0].uncertainty == 1.0  # kept the higher-priority (uncertain) one


def test_empty_queue() -> None:
    assert build_label_queue([]) == []
