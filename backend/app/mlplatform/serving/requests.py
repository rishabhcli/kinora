"""The serving workload: inference requests, their lifecycle state, and traffic.

A served LLM request is, for simulation purposes, a sequence that arrives at some
time, has a known prompt length (the prefill cost) and a generation length (how
many decode steps it will take), and threads through a lifecycle:

    QUEUED → (admitted) PREFILL → DECODING → DONE
                                      ↘ (cancelled / evicted) FAILED

The decode length is *latent* — in reality you do not know it until the model emits
a stop token — but for a deterministic simulation we draw it up front from a seeded
distribution so runs are reproducible. The scheduler must not peek at it for
admission decisions other than the ones it would legitimately make (it may use it
only to know when a request finishes, the way a real server learns at emit time).

This module is pure: the workload generator is fully seeded, so the same
:class:`WorkloadGenerator` config always yields the same request stream — essential
for property tests that assert invariants across "all generated workloads".
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from app.mlplatform.serving.contracts import _seeded_unit
from app.mlplatform.serving.errors import ServingConfigError


class RequestState(StrEnum):
    """Lifecycle state of a request inside the simulated server."""

    QUEUED = "queued"
    PREFILL = "prefill"
    DECODING = "decoding"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class InferenceRequest:
    """One request flowing through the simulated server.

    Immutable inputs (set at creation):

    * ``request_id`` — stable id.
    * ``arrival_ms`` — when it shows up at the scheduler.
    * ``prompt_tokens`` — prefill length.
    * ``max_tokens`` — the cap on generation; the request finishes at
      ``min(max_tokens, gen_tokens)``.
    * ``gen_tokens`` — the *latent* number of tokens the model will actually emit
      (drawn up front for reproducibility). The simulator reveals this only as the
      sequence decodes.
    * ``priority`` — lower is more urgent; ties break by arrival then id.

    Mutable runtime fields the simulator advances:

    * ``state`` — current :class:`RequestState`.
    * ``generated`` — tokens emitted so far.
    * ``admit_ms`` / ``first_token_ms`` / ``finish_ms`` — timestamps for metrics.
    """

    request_id: str
    arrival_ms: float
    prompt_tokens: int
    max_tokens: int
    gen_tokens: int
    priority: int = 0
    # runtime
    state: RequestState = RequestState.QUEUED
    generated: int = 0
    admit_ms: float | None = None
    first_token_ms: float | None = None
    finish_ms: float | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens <= 0:
            raise ServingConfigError("prompt_tokens must be positive")
        if self.max_tokens <= 0:
            raise ServingConfigError("max_tokens must be positive")
        if self.gen_tokens <= 0:
            raise ServingConfigError("gen_tokens must be positive")
        if self.arrival_ms < 0:
            raise ServingConfigError("arrival_ms must be non-negative")

    @property
    def target_tokens(self) -> int:
        """How many tokens this request will actually emit before stopping."""
        return min(self.max_tokens, self.gen_tokens)

    @property
    def remaining_tokens(self) -> int:
        """Decode steps still owed before the request finishes."""
        return max(0, self.target_tokens - self.generated)

    @property
    def total_context_tokens(self) -> int:
        """Prompt + generated-so-far — the live KV-cache footprint length."""
        return self.prompt_tokens + self.generated

    @property
    def is_terminal(self) -> bool:
        """Whether the request has left the active set."""
        return self.state in (RequestState.DONE, RequestState.FAILED)

    def sort_key(self) -> tuple[int, float, str]:
        """Deterministic scheduling order: priority, then arrival, then id."""
        return (self.priority, self.arrival_ms, self.request_id)


@dataclass(frozen=True, slots=True)
class WorkloadGenerator:
    """A seeded generator of a reproducible request stream.

    Models a Kinora-shaped read-ahead workload: requests arrive at a roughly steady
    rate (``arrival_rate_per_s``) with Poisson-ish jitter, prompts cluster around
    ``mean_prompt_tokens`` (the canon slice handed to the reasoning model), and
    generations around ``mean_gen_tokens`` (a shot plan / beat). All draws come from
    :func:`_seeded_unit`, so a given ``(seed, n)`` always yields the same stream.
    """

    seed: str = "kinora"
    n_requests: int = 64
    arrival_rate_per_s: float = 20.0
    mean_prompt_tokens: int = 512
    prompt_spread: int = 256
    mean_gen_tokens: int = 96
    gen_spread: int = 64
    max_tokens: int = 256
    priority_levels: int = 1

    def __post_init__(self) -> None:
        if self.n_requests < 0:
            raise ServingConfigError("n_requests must be non-negative")
        if self.arrival_rate_per_s <= 0:
            raise ServingConfigError("arrival_rate_per_s must be positive")
        if self.mean_prompt_tokens <= 0 or self.mean_gen_tokens <= 0:
            raise ServingConfigError("mean token counts must be positive")
        if self.priority_levels < 1:
            raise ServingConfigError("priority_levels must be >= 1")

    def generate(self) -> list[InferenceRequest]:
        """Produce the full request list, sorted by arrival time."""
        return list(self._iter())

    def _iter(self) -> Iterator[InferenceRequest]:
        clock = 0.0
        mean_gap_ms = 1000.0 / self.arrival_rate_per_s
        for i in range(self.n_requests):
            # Exponential-ish inter-arrival from a seeded uniform (inverse-CDF).
            u = max(1e-9, _seeded_unit(self.seed, "gap", str(i)))
            gap = -mean_gap_ms * math.log(u)
            clock += gap
            prompt = self._draw_int("prompt", i, self.mean_prompt_tokens, self.prompt_spread, lo=8)
            gen = self._draw_int("gen", i, self.mean_gen_tokens, self.gen_spread, lo=1)
            prio = int(_seeded_unit(self.seed, "prio", str(i)) * self.priority_levels)
            yield InferenceRequest(
                request_id=f"{self.seed}-req-{i:05d}",
                arrival_ms=round(clock, 3),
                prompt_tokens=prompt,
                max_tokens=self.max_tokens,
                gen_tokens=min(gen, self.max_tokens),
                priority=min(prio, self.priority_levels - 1),
            )

    def _draw_int(self, label: str, i: int, mean: int, spread: int, *, lo: int) -> int:
        """A seeded triangular-ish integer draw centered on ``mean``."""
        # Average two uniforms → triangular distribution on [-1, 1], scale by spread.
        u1 = _seeded_unit(self.seed, label, str(i), "a")
        u2 = _seeded_unit(self.seed, label, str(i), "b")
        centered = (u1 + u2) - 1.0  # in [-1, 1], peaked at 0
        value = mean + int(round(centered * spread))
        return max(lo, value)


__all__ = [
    "InferenceRequest",
    "RequestState",
    "WorkloadGenerator",
]
