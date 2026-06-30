"""Deterministic sampling: fraction correctness, determinism, monotone inclusion."""

from __future__ import annotations

from app.video.shadow.sampler import AlwaysSampler, DeterministicSampler


def test_zero_fraction_samples_nothing() -> None:
    sampler = DeterministicSampler(0.0)
    assert sampler.fraction == 0.0
    assert not any(sampler.in_sample(f"shot-{i}") for i in range(1000))


def test_full_fraction_samples_everything() -> None:
    sampler = DeterministicSampler(1.0)
    assert sampler.fraction == 1.0
    assert all(sampler.in_sample(f"shot-{i}") for i in range(1000))


def test_fraction_is_clamped() -> None:
    assert DeterministicSampler(-0.5).fraction == 0.0
    assert DeterministicSampler(5.0).fraction == 1.0


def test_sampling_is_deterministic_per_shot() -> None:
    a = DeterministicSampler(0.5)
    b = DeterministicSampler(0.5)
    for i in range(500):
        key = f"shot-{i}"
        # Same key, repeated and across instances, always the same decision.
        assert a.in_sample(key) == a.in_sample(key) == b.in_sample(key)


def test_fraction_is_approximately_correct() -> None:
    # Over a large key space the observed rate should be close to the target.
    n = 20_000
    for target in (0.1, 0.25, 0.5, 0.75):
        sampler = DeterministicSampler(target)
        hits = sum(sampler.in_sample(f"shot-{i}") for i in range(n))
        observed = hits / n
        assert abs(observed - target) < 0.02, (target, observed)


def test_widening_fraction_only_adds_shots() -> None:
    # Monotone inclusion: every shot sampled at 0.2 is still sampled at 0.6.
    narrow = DeterministicSampler(0.2)
    wide = DeterministicSampler(0.6)
    keys = [f"shot-{i}" for i in range(5_000)]
    sampled_narrow = {k for k in keys if narrow.in_sample(k)}
    sampled_wide = {k for k in keys if wide.in_sample(k)}
    assert sampled_narrow <= sampled_wide
    assert len(sampled_wide) > len(sampled_narrow)


def test_salt_decorrelates_candidate_sets() -> None:
    # Two candidates at the same fraction but different salts evaluate
    # meaningfully different (not identical) shot sets.
    a = DeterministicSampler(0.5, salt="cand-a")
    b = DeterministicSampler(0.5, salt="cand-b")
    keys = [f"shot-{i}" for i in range(5_000)]
    set_a = {k for k in keys if a.in_sample(k)}
    set_b = {k for k in keys if b.in_sample(k)}
    # Substantial symmetric difference (they are not the same sample).
    symmetric_diff = set_a ^ set_b
    assert len(symmetric_diff) > 0.3 * len(keys)


def test_always_sampler_samples_everything() -> None:
    sampler = AlwaysSampler()
    assert all(sampler.in_sample(f"x{i}") for i in range(100))
