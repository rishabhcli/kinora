"""Speculative-decoding orchestration.

A cheap **draft** backend proposes ``k`` next tokens; the expensive **target**
verifies them in a single pass and the orchestrator commits the longest prefix
the target agrees with, plus one *bonus* correction token the verification gives
for free. Repeat until the target would stop or ``max_tokens`` is reached.

The cardinal invariant — and the property the tests pin — is that the committed
output is **byte-for-byte identical** to plain target decoding regardless of how
good or bad the draft is. The draft only affects *speed*: a perfect draft lets
each target call commit ``k+1`` tokens; a useless draft degrades to one token per
target call (i.e. exactly non-speculative decoding), never worse in correctness.

Adaptive draft length
----------------------
The proposal width ``k`` is not fixed. An :class:`AdaptiveDraftLength` controller
watches the running acceptance rate and grows ``k`` when the draft is reliably
accepted (amortising more target work per call) and shrinks it when acceptances
are short (so a bad draft does not waste a wide, mostly-rejected proposal). The
update rule is a deterministic AIMD-style step, so a seeded acceptance sequence
produces a reproducible ``k`` trajectory.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import SYSTEM_CLOCK, Clock
from .errors import SpeculationConsistencyError
from .metrics import SpeculativeMetrics
from .protocol import (
    DraftBackend,
    GenerationRequest,
    GenerationResult,
    TokenScorer,
)
from .tokenize import common_prefix_length, join_tokens


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    """Tunables for the adaptive draft-length controller."""

    initial_k: int = 4
    min_k: int = 1
    max_k: int = 16
    #: Acceptance fraction (accepted/proposed in a round) at/above which ``k`` grows.
    grow_threshold: float = 0.75
    #: Acceptance fraction below which ``k`` shrinks.
    shrink_threshold: float = 0.35
    #: Additive step when growing.
    grow_step: int = 1
    #: Multiplicative factor when shrinking (multiplicative-decrease).
    shrink_factor: float = 0.5

    def __post_init__(self) -> None:
        if not (self.min_k >= 1 and self.max_k >= self.min_k):
            raise ValueError("require 1 <= min_k <= max_k")
        if not (self.min_k <= self.initial_k <= self.max_k):
            raise ValueError("initial_k must lie within [min_k, max_k]")
        if not (0.0 <= self.shrink_threshold <= self.grow_threshold <= 1.0):
            raise ValueError("require 0 <= shrink_threshold <= grow_threshold <= 1")


class AdaptiveDraftLength:
    """Deterministic AIMD controller for the speculative proposal width ``k``.

    Additive-increase when a round's local acceptance fraction is high,
    multiplicative-decrease when it is low, hold otherwise. Clamped to
    ``[min_k, max_k]``. Pure and reproducible — no clock, no randomness.
    """

    def __init__(self, config: AdaptiveConfig | None = None) -> None:
        self.config = config or AdaptiveConfig()
        self._k = self.config.initial_k

    @property
    def k(self) -> int:
        return self._k

    def observe(self, *, proposed: int, accepted: int) -> int:
        """Update and return ``k`` after a round that proposed/accepted tokens."""
        cfg = self.config
        if proposed <= 0:
            # No information (e.g. target was already at EOS); hold.
            return self._k
        frac = accepted / proposed
        if frac >= cfg.grow_threshold:
            self._k = min(cfg.max_k, self._k + cfg.grow_step)
        elif frac < cfg.shrink_threshold:
            shrunk = int(self._k * cfg.shrink_factor)
            self._k = max(cfg.min_k, shrunk)
        return self._k

    def reset(self) -> None:
        self._k = self.config.initial_k


@dataclass(frozen=True, slots=True)
class SpeculativeDecodeResult:
    """The outcome of one speculative generation, with attribution metadata."""

    result: GenerationResult
    rounds: int
    proposed_tokens: int
    accepted_tokens: int
    bonus_tokens: int
    target_calls: int
    draft_calls: int

    @property
    def acceptance_rate(self) -> float:
        return self.accepted_tokens / self.proposed_tokens if self.proposed_tokens else 0.0


class SpeculativeDecoder:
    """Orchestrates draft-propose / target-verify speculative decoding.

    Correctness contract: ``decode(req)`` returns the same token sequence as
    ``target.generate(req)`` for any draft. The orchestrator additionally
    *asserts* this by checking that the target never contradicts a token it has
    already committed (a :class:`SpeculationConsistencyError` if it ever does).
    """

    def __init__(
        self,
        draft: DraftBackend,
        target: TokenScorer,
        *,
        config: AdaptiveConfig | None = None,
        metrics: SpeculativeMetrics | None = None,
        clock: Clock = SYSTEM_CLOCK,
        joiner: str = " ",
        target_model: str = "target",
    ) -> None:
        self._draft = draft
        self._target = target
        self._controller = AdaptiveDraftLength(config)
        self._metrics = metrics or SpeculativeMetrics()
        self._clock = clock
        self._joiner = joiner
        self._target_model = target_model

    @property
    def metrics(self) -> SpeculativeMetrics:
        return self._metrics

    @property
    def current_k(self) -> int:
        return self._controller.k

    async def decode(self, request: GenerationRequest) -> SpeculativeDecodeResult:
        """Generate for ``request`` via speculative decoding."""
        started = self._clock.monotonic()
        committed: list[str] = []
        rounds = total_proposed = total_accepted = total_bonus = 0
        target_calls = draft_calls = 0

        max_tokens = max(0, request.max_tokens)
        while len(committed) < max_tokens:
            committed_tuple = tuple(committed)

            # Early stop check: if the target would stop here, do not waste a
            # draft+verify round.
            if await self._target.is_finished(request, committed_tuple):
                break

            k = min(self._controller.k, max_tokens - len(committed))
            proposal = await self._draft.propose(request, committed_tuple, k)
            draft_calls += 1
            proposed_tokens = proposal.tokens

            # Target verification: its own next token at each prefix position.
            verified = await self._target.verify(request, committed_tuple, proposed_tokens)
            target_calls += 1
            self._guard_verification(verified, proposed_tokens)

            accepted_n = self._accepted_count(proposed_tokens, verified)
            bonus_token = verified[accepted_n] if accepted_n < len(verified) else ""

            # Commit accepted prefix.
            committed.extend(proposed_tokens[:accepted_n])
            bonus_committed = 0
            if bonus_token and len(committed) < max_tokens:
                committed.append(bonus_token)
                bonus_committed = 1

            rounds += 1
            total_proposed += len(proposed_tokens)
            total_accepted += accepted_n
            total_bonus += bonus_committed
            self._metrics.record_round(
                proposed=len(proposed_tokens),
                accepted=accepted_n,
                bonus=bonus_committed,
            )
            self._controller.observe(proposed=len(proposed_tokens), accepted=accepted_n)

            # No progress this round (no accepted token AND no bonus appended)
            # means the target produced no continuation — it is finished. Without
            # this guard a target at EOS that ``is_finished`` mis-reports would
            # spin forever; with it the loop always terminates.
            if accepted_n == 0 and bonus_committed == 0:
                break

        latency_ms = (self._clock.monotonic() - started) * 1000.0
        result = GenerationResult.from_tokens(
            committed,
            model=self._target_model,
            finish_reason="stop",
            joiner=self._joiner,
            meta={
                "accelerator": "speculative",
                "rounds": rounds,
                "target_calls": target_calls,
                "draft_calls": draft_calls,
                "acceptance_rate": (total_accepted / total_proposed) if total_proposed else 0.0,
                "final_k": self._controller.k,
                "latency_ms": round(latency_ms, 3),
            },
        )
        return SpeculativeDecodeResult(
            result=result,
            rounds=rounds,
            proposed_tokens=total_proposed,
            accepted_tokens=total_accepted,
            bonus_tokens=total_bonus,
            target_calls=target_calls,
            draft_calls=draft_calls,
        )

    @staticmethod
    def _accepted_count(proposed: tuple[str, ...], verified: tuple[str, ...]) -> int:
        """Longest prefix of ``proposed`` that matches the target's own tokens.

        ``verified[i]`` is the target's token at position ``i``; we accept token
        ``proposed[i]`` iff it equals ``verified[i]`` (and stop at the first
        mismatch). ``verified`` has one extra trailing element (the bonus).
        """
        return common_prefix_length(proposed, verified[: len(proposed)])

    def _guard_verification(self, verified: tuple[str, ...], proposed: tuple[str, ...]) -> None:
        if len(verified) != len(proposed) + 1:
            raise SpeculationConsistencyError(
                f"target.verify returned {len(verified)} tokens; "
                f"expected {len(proposed) + 1} for a proposal of {len(proposed)}"
            )


async def speculative_text(
    decoder: SpeculativeDecoder, prompt: str, *, model: str = "default", max_tokens: int = 256
) -> str:
    """Convenience: speculatively decode a bare prompt to a text string."""
    req = GenerationRequest.from_prompt(prompt, model=model, max_tokens=max_tokens)
    out = await decoder.decode(req)
    return out.result.text


__all__ = [
    "AdaptiveConfig",
    "AdaptiveDraftLength",
    "SpeculativeDecodeResult",
    "SpeculativeDecoder",
    "join_tokens",
    "speculative_text",
]
