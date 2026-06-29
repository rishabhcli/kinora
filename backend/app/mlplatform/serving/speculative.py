"""Speculative decoding model: a draft model proposes, the target verifies.

Speculative decoding speeds up autoregressive generation by having a small, cheap
*draft* model propose ``k`` tokens ahead, then having the large *target* model
verify all ``k`` in a single forward pass. The target accepts the longest correct
prefix of the proposal and emits one extra "bonus" token; rejected tokens are
discarded and re-drawn. The win: when the draft is usually right, the target emits
several tokens per (expensive) target step instead of one.

This module models the *throughput multiplier* analytically and deterministically.
Given a per-token draft acceptance probability ``alpha`` and a speculation length
``k``, the expected number of tokens the target commits per verification step is the
standard geometric-acceptance result:

    E[accepted] = (1 - alpha^(k+1)) / (1 - alpha)        (alpha < 1)
                = k + 1                                    (alpha == 1)

The effective cost per committed token is then one target step (the verify pass)
plus ``k`` cheap draft steps, amortized over ``E[accepted]`` committed tokens. We
return both the speedup vs. plain decoding and the per-committed-token wall-clock so
the simulator's decode model can use speculative mode transparently.

Pure math — no sampling, no randomness. For a *simulated* per-request acceptance we
derive a deterministic accepted-count from a seeded draw so individual requests vary
while the aggregate matches the analytic mean.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.mlplatform.serving.contracts import _seeded_unit
from app.mlplatform.serving.errors import ServingConfigError


@dataclass(frozen=True, slots=True)
class SpeculativeConfig:
    """Configuration for speculative decoding.

    ``k`` is the speculation length (draft tokens proposed per target verify).
    ``alpha`` is the per-token acceptance probability (how often the target agrees
    with the draft). ``draft_cost_ratio`` is the draft model's per-token decode cost
    as a fraction of the target's (a 1B draft for a 70B target might be ~0.05).
    ``enabled`` lets a serving config carry a disabled spec-decode block.
    """

    enabled: bool = False
    k: int = 4
    alpha: float = 0.7
    draft_cost_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ServingConfigError("speculative k must be >= 1")
        if not 0.0 <= self.alpha <= 1.0:
            raise ServingConfigError("alpha must be in [0, 1]")
        if not 0.0 <= self.draft_cost_ratio < 1.0:
            raise ServingConfigError("draft_cost_ratio must be in [0, 1)")


def expected_accepted(k: int, alpha: float) -> float:
    """Expected committed tokens per verify step under geometric acceptance.

    Includes the bonus token on a fully-accepted block. Always in ``[1, k+1]``.
    """
    if k < 1:
        raise ServingConfigError("k must be >= 1")
    if not 0.0 <= alpha <= 1.0:
        raise ServingConfigError("alpha must be in [0, 1]")
    if alpha >= 1.0:
        return float(k + 1)
    # sum_{i=0}^{k} alpha^i  ==  (1 - alpha^(k+1)) / (1 - alpha)
    return (1.0 - alpha ** (k + 1)) / (1.0 - alpha)


@dataclass(frozen=True, slots=True)
class SpeculativeOutcome:
    """The analytic outcome of running speculative decode with a config."""

    expected_tokens_per_step: float
    cost_per_token_ratio: float  # vs. plain target decode (1.0 = no win)
    speedup: float  # plain_cost / spec_cost

    def __post_init__(self) -> None:
        if self.expected_tokens_per_step <= 0:
            raise ServingConfigError("expected_tokens_per_step must be positive")


class SpeculativeDecoder:
    """Deterministic speculative-decoding model the simulator's decoder consumes."""

    def __init__(self, config: SpeculativeConfig) -> None:
        self.config = config

    def outcome(self) -> SpeculativeOutcome:
        """The steady-state analytic outcome (no per-request variance).

        Plain decoding costs 1 target step per token. Speculative decoding costs one
        target verify step plus ``k`` draft steps per *block*, committing
        ``E[accepted]`` tokens — so per-committed-token cost is
        ``(1 + k * draft_cost_ratio) / E[accepted]`` target-step units.
        """
        cfg = self.config
        e_acc = expected_accepted(cfg.k, cfg.alpha)
        block_cost = 1.0 + cfg.k * cfg.draft_cost_ratio
        cost_per_token = block_cost / e_acc
        speedup = 1.0 / cost_per_token
        return SpeculativeOutcome(
            expected_tokens_per_step=e_acc,
            cost_per_token_ratio=cost_per_token,
            speedup=speedup,
        )

    def simulate_block(self, request_id: str, step: int) -> int:
        """Deterministically draw how many of ``k`` proposed tokens were accepted.

        Returns committed tokens in ``[1, k+1]``: a seeded geometric-style draw so
        individual blocks vary (some reject early, some accept all + bonus) while the
        long-run average tracks :func:`expected_accepted`. Used by the simulator when
        it wants per-request texture rather than the steady-state mean.
        """
        cfg = self.config
        accepted = 0
        for i in range(cfg.k):
            u = _seeded_unit(request_id, "spec", str(step), str(i))
            if u <= cfg.alpha:
                accepted += 1
            else:
                break
        # Bonus token only when the whole block was accepted.
        if accepted == cfg.k:
            return cfg.k + 1
        return accepted + 1  # the target's own correction token is always committed

    def is_active(self) -> bool:
        return self.config.enabled


__all__ = [
    "SpeculativeConfig",
    "SpeculativeDecoder",
    "SpeculativeOutcome",
    "expected_accepted",
]
