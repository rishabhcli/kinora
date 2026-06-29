"""Speculative-decoding tests.

The headline proof: speculative output == plain target decoding, for a perfect
draft, a partially-correct draft, and a useless draft. Plus the adaptive
controller trajectory and metrics accounting.
"""

from __future__ import annotations

import random

import pytest

from app.inference.accel.clock import FakeClock
from app.inference.accel.errors import SpeculationConsistencyError
from app.inference.accel.fakes import ScriptedDraft, ScriptedTarget
from app.inference.accel.metrics import SpeculativeMetrics
from app.inference.accel.protocol import GenerationRequest
from app.inference.accel.speculative import (
    AdaptiveConfig,
    AdaptiveDraftLength,
    SpeculativeDecoder,
    speculative_text,
)

SENTENCE = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"]


def _oracle(_req: GenerationRequest) -> list[str]:
    return list(SENTENCE)


def _decoder(draft: ScriptedDraft, *, config: AdaptiveConfig | None = None) -> SpeculativeDecoder:
    target = ScriptedTarget(default_fn=_oracle)
    return SpeculativeDecoder(
        draft,
        target,
        config=config,
        metrics=SpeculativeMetrics(),
        clock=FakeClock(),
    )


# --------------------------------------------------------------------------- #
# Correctness equivalence — the cardinal invariant
# --------------------------------------------------------------------------- #


async def test_perfect_draft_matches_target() -> None:
    draft = ScriptedDraft(oracle=_oracle)
    dec = _decoder(draft)
    req = GenerationRequest.from_prompt("x", max_tokens=20)
    out = await dec.decode(req)
    assert out.result.tokens == tuple(SENTENCE)
    # A perfect draft means almost every proposed token is accepted.
    assert out.acceptance_rate == pytest.approx(1.0)
    # And far fewer target calls than tokens (the speedup).
    assert out.target_calls < len(SENTENCE)


async def test_useless_draft_still_matches_target() -> None:
    # Draft proposes only wrong tokens (corrupt at offset 0 + stop_after small)
    bad_oracle = lambda r: ["zzz", "yyy", "xxx", "www"]  # noqa: E731
    draft = ScriptedDraft(oracle=bad_oracle)
    dec = _decoder(draft)
    req = GenerationRequest.from_prompt("x", max_tokens=20)
    out = await dec.decode(req)
    # Output is STILL the target's sequence — correctness is independent of draft.
    assert out.result.tokens == tuple(SENTENCE)
    # Useless draft -> each round commits only the bonus correction token,
    # i.e. one token per target call -> degrades to non-speculative.
    assert out.target_calls == len(SENTENCE)
    assert out.acceptance_rate == 0.0


async def test_partial_draft_matches_target() -> None:
    # Draft corrupts the 3rd global token, forcing a mid-round rejection.
    draft = ScriptedDraft(oracle=_oracle, corrupt_at=3)
    dec = _decoder(draft)
    req = GenerationRequest.from_prompt("x", max_tokens=20)
    out = await dec.decode(req)
    assert out.result.tokens == tuple(SENTENCE)
    assert 0.0 < out.acceptance_rate < 1.0


async def test_equivalence_over_random_draft_corruptions() -> None:
    """Fuzz: every random corruption point still yields the exact target output."""
    rng = random.Random(1234)
    target = ScriptedTarget(default_fn=_oracle)
    expected = (await target.generate(GenerationRequest.from_prompt("x", max_tokens=50))).tokens
    for _ in range(50):
        corrupt = rng.randint(0, len(SENTENCE) - 1)
        stop = rng.randint(1, len(SENTENCE))
        draft = ScriptedDraft(oracle=_oracle, corrupt_at=corrupt, stop_after=stop)
        dec = _decoder(draft, config=AdaptiveConfig(initial_k=rng.randint(1, 8)))
        out = await dec.decode(GenerationRequest.from_prompt("x", max_tokens=50))
        assert out.result.tokens == expected


async def test_max_tokens_truncates() -> None:
    draft = ScriptedDraft(oracle=_oracle)
    dec = _decoder(draft)
    out = await dec.decode(GenerationRequest.from_prompt("x", max_tokens=3))
    assert out.result.tokens == ("the", "quick", "brown")


async def test_speculative_text_helper() -> None:
    draft = ScriptedDraft(oracle=_oracle)
    dec = _decoder(draft)
    text = await speculative_text(dec, "x", max_tokens=20)
    assert text == "the quick brown fox jumps over the lazy dog"


async def test_empty_target_stops_immediately() -> None:
    target = ScriptedTarget(default_fn=lambda r: [])
    draft = ScriptedDraft(oracle=lambda r: ["a", "b"])
    dec = SpeculativeDecoder(draft, target, clock=FakeClock())
    out = await dec.decode(GenerationRequest.from_prompt("x", max_tokens=10))
    assert out.result.tokens == ()
    assert out.target_calls <= 1


async def test_metrics_recorded_and_meta_populated() -> None:
    draft = ScriptedDraft(oracle=_oracle)
    dec = _decoder(draft)
    out = await dec.decode(GenerationRequest.from_prompt("x", max_tokens=20))
    snap = dec.metrics.snapshot()
    assert snap.committed_tokens == len(SENTENCE)
    assert out.result.meta["accelerator"] == "speculative"
    assert out.result.meta["rounds"] == out.rounds
    assert "latency_ms" in out.result.meta


# --------------------------------------------------------------------------- #
# Consistency guard
# --------------------------------------------------------------------------- #


async def test_inconsistent_verify_raises() -> None:
    class BrokenTarget(ScriptedTarget):
        async def verify(self, request, committed, proposal):  # type: ignore[no-untyped-def]
            # Wrong length: returns len(proposal) instead of len(proposal)+1.
            return proposal

    target = BrokenTarget(default_fn=_oracle)
    draft = ScriptedDraft(oracle=_oracle)
    dec = SpeculativeDecoder(draft, target, clock=FakeClock())
    with pytest.raises(SpeculationConsistencyError):
        await dec.decode(GenerationRequest.from_prompt("x", max_tokens=5))


# --------------------------------------------------------------------------- #
# Adaptive draft-length controller
# --------------------------------------------------------------------------- #


def test_adaptive_grows_on_high_acceptance() -> None:
    ctrl = AdaptiveDraftLength(AdaptiveConfig(initial_k=4, max_k=8, grow_step=1))
    k = ctrl.observe(proposed=4, accepted=4)  # 100% -> grow
    assert k == 5
    for _ in range(10):
        ctrl.observe(proposed=4, accepted=4)
    assert ctrl.k == 8  # clamped at max


def test_adaptive_shrinks_on_low_acceptance() -> None:
    ctrl = AdaptiveDraftLength(AdaptiveConfig(initial_k=8, min_k=1, shrink_factor=0.5))
    k = ctrl.observe(proposed=8, accepted=0)  # 0% -> shrink
    assert k == 4
    ctrl.observe(proposed=8, accepted=0)
    assert ctrl.k == 2
    for _ in range(5):
        ctrl.observe(proposed=8, accepted=0)
    assert ctrl.k == 1  # clamped at min


def test_adaptive_holds_in_band() -> None:
    cfg = AdaptiveConfig(initial_k=4, grow_threshold=0.75, shrink_threshold=0.35)
    ctrl = AdaptiveDraftLength(cfg)
    ctrl.observe(proposed=4, accepted=2)  # 0.5 -> in band, hold
    assert ctrl.k == 4


def test_adaptive_zero_proposed_holds() -> None:
    ctrl = AdaptiveDraftLength(AdaptiveConfig(initial_k=3))
    assert ctrl.observe(proposed=0, accepted=0) == 3


def test_adaptive_config_validation() -> None:
    with pytest.raises(ValueError):
        AdaptiveConfig(min_k=0)
    with pytest.raises(ValueError):
        AdaptiveConfig(initial_k=99, max_k=8)
    with pytest.raises(ValueError):
        AdaptiveConfig(grow_threshold=0.2, shrink_threshold=0.5)


def test_adaptive_reset() -> None:
    ctrl = AdaptiveDraftLength(AdaptiveConfig(initial_k=4))
    ctrl.observe(proposed=4, accepted=4)
    assert ctrl.k == 5
    ctrl.reset()
    assert ctrl.k == 4


async def test_adaptive_k_grows_during_perfect_decode() -> None:
    # Long sentence + perfect draft -> controller should ratchet k upward.
    long_oracle = lambda r: [f"w{i}" for i in range(40)]  # noqa: E731
    target = ScriptedTarget(default_fn=long_oracle)
    draft = ScriptedDraft(oracle=long_oracle)
    dec = SpeculativeDecoder(
        draft, target, config=AdaptiveConfig(initial_k=2, max_k=10), clock=FakeClock()
    )
    out = await dec.decode(GenerationRequest.from_prompt("x", max_tokens=40))
    assert out.result.tokens == tuple(long_oracle(None))
    assert dec.current_k > 2  # grew from repeated full acceptance
