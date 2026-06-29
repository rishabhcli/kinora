"""Unit + property tests for speculative decoding (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.contracts import _seeded_unit
from app.mlplatform.serving.errors import ServingConfigError
from app.mlplatform.serving.speculative import (
    SpeculativeConfig,
    SpeculativeDecoder,
    expected_accepted,
)


def test_config_validation() -> None:
    with pytest.raises(ServingConfigError):
        SpeculativeConfig(k=0)
    with pytest.raises(ServingConfigError):
        SpeculativeConfig(alpha=1.5)
    with pytest.raises(ServingConfigError):
        SpeculativeConfig(draft_cost_ratio=1.0)


def test_expected_accepted_known_values() -> None:
    # alpha == 1 → always accept all k plus the bonus.
    assert expected_accepted(4, 1.0) == 5.0
    # alpha == 0 → never accept a proposal; the one correction token still commits.
    assert expected_accepted(4, 0.0) == pytest.approx(1.0)
    # General geometric series midpoint.
    assert expected_accepted(2, 0.5) == pytest.approx(1 + 0.5 + 0.25)


def test_expected_accepted_is_monotonic_in_alpha() -> None:
    prev = 0.0
    for alpha in [i / 20 for i in range(21)]:
        e = expected_accepted(4, alpha)
        assert e >= prev
        assert 1.0 <= e <= 5.0
        prev = e


def test_expected_accepted_rejects_bad_inputs() -> None:
    with pytest.raises(ServingConfigError):
        expected_accepted(0, 0.5)
    with pytest.raises(ServingConfigError):
        expected_accepted(4, 1.1)


def test_outcome_speedup_above_one_for_good_draft() -> None:
    dec = SpeculativeDecoder(SpeculativeConfig(enabled=True, k=4, alpha=0.8, draft_cost_ratio=0.05))
    out = dec.outcome()
    assert out.speedup > 1.0
    assert out.expected_tokens_per_step > 1.0
    assert 0.0 < out.cost_per_token_ratio < 1.0


def test_outcome_no_win_when_draft_useless() -> None:
    # alpha=0 → 1 token per block but still pay draft cost → slower than plain.
    dec = SpeculativeDecoder(SpeculativeConfig(enabled=True, k=4, alpha=0.0, draft_cost_ratio=0.2))
    out = dec.outcome()
    assert out.speedup < 1.0


def test_simulate_block_in_range_and_deterministic() -> None:
    dec = SpeculativeDecoder(SpeculativeConfig(enabled=True, k=4, alpha=0.7))
    for step in range(50):
        c1 = dec.simulate_block("req-1", step)
        c2 = dec.simulate_block("req-1", step)
        assert c1 == c2
        assert 1 <= c1 <= 5


def test_is_active_reflects_config() -> None:
    assert SpeculativeDecoder(SpeculativeConfig(enabled=False)).is_active() is False
    assert SpeculativeDecoder(SpeculativeConfig(enabled=True)).is_active() is True


@pytest.mark.parametrize("alpha", [0.3, 0.5, 0.7, 0.9])
def test_property_simulated_mean_tracks_analytic(alpha: float) -> None:
    """Over many seeded blocks the empirical mean accepted ≈ the analytic E[accepted].

    Property: the deterministic per-block draw is unbiased w.r.t. the closed-form
    expectation (within a tolerance for the finite sample).
    """
    k = 4
    cfg = SpeculativeConfig(enabled=True, k=k, alpha=alpha)
    dec = SpeculativeDecoder(cfg)
    n = 4000
    total = sum(dec.simulate_block(f"r{_seeded_unit('r', str(i))}", i) for i in range(n))
    empirical = total / n
    analytic = expected_accepted(k, alpha)
    # The per-block draw uses a fresh seed per (req, step, i), so the empirical mean
    # converges to the analytic mean. Allow a generous tolerance for the sample size.
    assert empirical == pytest.approx(analytic, abs=0.2)
