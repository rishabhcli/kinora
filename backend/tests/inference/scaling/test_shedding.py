"""Unit tests for graceful load-shedding (app.inference.scaling.shedding)."""

from __future__ import annotations

import pytest

from app.inference.scaling.shedding import (
    AdmissionOutcome,
    LoadShedder,
    SheddingPolicy,
)
from app.inference.scaling.workload import RequestPriority

# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_shed_probability_is_linear_ramp() -> None:
    p = SheddingPolicy(shed_knee=0.5, shed_ceiling=1.0)
    assert p.shed_probability(0.4) == 0.0
    assert p.shed_probability(0.5) == 0.0
    assert p.shed_probability(0.75) == pytest.approx(0.5)
    assert p.shed_probability(1.0) == 1.0
    assert p.shed_probability(2.0) == 1.0  # clamped


def test_policy_rejects_bad_band() -> None:
    with pytest.raises(ValueError):
        SheddingPolicy(shed_knee=0.9, shed_ceiling=0.5)
    with pytest.raises(ValueError):
        SheddingPolicy(max_queue=0)


# --------------------------------------------------------------------------- #
# Committed protection
# --------------------------------------------------------------------------- #


def test_committed_never_shed_under_saturation() -> None:
    s = LoadShedder(SheddingPolicy(shed_knee=0.0, shed_ceiling=0.01), seed=1)
    for _ in range(50):
        d = s.admit(
            priority=RequestPriority.COMMITTED, saturation=1.0, outstanding=0, can_serve_now=False
        )
        assert d.admitted
    assert s.shed == 0


def test_committed_shed_only_at_global_cap() -> None:
    s = LoadShedder(SheddingPolicy(max_queue=10), seed=1)
    d = s.admit(
        priority=RequestPriority.COMMITTED, saturation=0.0, outstanding=10, can_serve_now=True
    )
    assert d.outcome is AdmissionOutcome.SHED
    assert "queue cap" in d.reason


# --------------------------------------------------------------------------- #
# Speculative shedding
# --------------------------------------------------------------------------- #


def test_speculative_never_shed_below_knee() -> None:
    s = LoadShedder(SheddingPolicy(shed_knee=0.8, shed_ceiling=0.99), seed=2)
    for _ in range(100):
        d = s.admit(priority=RequestPriority.SPECULATIVE, saturation=0.5, outstanding=0)
        assert d.admitted
    assert s.shed == 0


def test_speculative_always_shed_at_ceiling() -> None:
    s = LoadShedder(SheddingPolicy(shed_knee=0.7, shed_ceiling=0.9), seed=2)
    for _ in range(50):
        d = s.admit(priority=RequestPriority.SPECULATIVE, saturation=0.95, outstanding=0)
        assert d.outcome is AdmissionOutcome.SHED


def test_speculative_partial_shed_in_band() -> None:
    # Mid-band saturation sheds *some* speculative work (probabilistic).
    s = LoadShedder(SheddingPolicy(shed_knee=0.5, shed_ceiling=1.0), seed=3)
    n = 2000
    for _ in range(n):
        s.admit(priority=RequestPriority.SPECULATIVE, saturation=0.75, outstanding=0)
    # shed_probability(0.75) == 0.5 → roughly half shed.
    assert 0.4 <= s.shed_rate <= 0.6


def test_shed_is_deterministic_given_seed() -> None:
    def run() -> int:
        s = LoadShedder(SheddingPolicy(shed_knee=0.5, shed_ceiling=1.0), seed=99)
        for _ in range(500):
            s.admit(priority=RequestPriority.SPECULATIVE, saturation=0.8, outstanding=0)
        return s.shed

    assert run() == run()


def test_global_cap_sheds_speculative_too() -> None:
    s = LoadShedder(SheddingPolicy(max_queue=5), seed=1)
    d = s.admit(priority=RequestPriority.SPECULATIVE, saturation=0.0, outstanding=5)
    assert d.outcome is AdmissionOutcome.SHED


def test_decision_to_dict() -> None:
    s = LoadShedder(seed=1)
    d = s.admit(priority=RequestPriority.COMMITTED, saturation=0.3, outstanding=0)
    payload = d.to_dict()
    assert payload["outcome"] == "admit"
    assert payload["priority"] == "committed"
