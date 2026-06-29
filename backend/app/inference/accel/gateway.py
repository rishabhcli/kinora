"""The composed acceleration gateway — one :class:`InferenceBackend` that stacks
every accel facet on top of a base backend.

The layering, outermost to innermost, for a :meth:`generate` call:

1. **Semantic cache** — exact-prefix then embedding-similarity lookup. A hit
   returns immediately at zero backend cost.
2. **Prefix/KV reuse** — on a miss, the prompt is planned against the resident
   prefix book so the savings are recorded (the bookkeeping the serving engine
   would act on); the registered scaffold is reused by later calls.
3. **Speculative decoding** — if a draft + target are configured, the miss is
   served speculatively (output identical to the target); otherwise the base
   backend is called directly.
4. The fresh result is stored back into the semantic cache.

Fan-out racing and constrained decoding are *orthogonal* entry points
(:meth:`race` and :meth:`generate_constrained`) rather than always-on layers,
because they change the call's contract (multiple providers / a required
schema). Each can itself be cache-wrapped by the caller.

Every layer is optional and individually injectable, so a caller can take just
the cache, just speculation, or the full stack. The gateway is itself an
:class:`InferenceBackend`, so accelerated backends nest.
"""

from __future__ import annotations

from collections.abc import Sequence

from .clock import SYSTEM_CLOCK, Clock
from .constrained import ConstrainedDecoder, ConstrainedResult, Constraint
from .fanout import FanOutRacer, FanOutResult, ProviderCandidate, Validator
from .prefix_reuse import KVReuseBook
from .protocol import (
    DraftBackend,
    GenerationRequest,
    GenerationResult,
    InferenceBackend,
    TokenScorer,
)
from .semantic_cache import SemanticCache
from .speculative import AdaptiveConfig, SpeculativeDecoder
from .tokenize import word_tokens


class AcceleratedGateway:
    """Composes semantic cache + prefix reuse + speculative decoding over a base
    :class:`InferenceBackend`.

    Args:
        base: The wrapped backend; used directly when speculation is not
            configured, and as the cache's compute-on-miss path.
        cache: Optional semantic cache. When present, ``generate`` is read-
            through / write-on-miss.
        draft / target: Optional speculative-decoding pair. When both are given,
            misses are served speculatively.
        prefix_book: Optional KV reuse bookkeeping. When present, every miss's
            prompt is planned + registered so reuse is tracked.
        namespace: Cache namespace for this gateway's traffic.
    """

    def __init__(
        self,
        base: InferenceBackend,
        *,
        cache: SemanticCache | None = None,
        draft: DraftBackend | None = None,
        target: TokenScorer | None = None,
        speculative_config: AdaptiveConfig | None = None,
        prefix_book: KVReuseBook | None = None,
        namespace: str = "default",
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._base = base
        self._cache = cache
        self._prefix_book = prefix_book
        self._namespace = namespace
        self._clock = clock
        self._speculative: SpeculativeDecoder | None = None
        if draft is not None and target is not None:
            self._speculative = SpeculativeDecoder(
                draft, target, config=speculative_config, clock=clock
            )

    @property
    def cache(self) -> SemanticCache | None:
        return self._cache

    @property
    def speculative(self) -> SpeculativeDecoder | None:
        return self._speculative

    @property
    def prefix_book(self) -> KVReuseBook | None:
        return self._prefix_book

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Accelerated generation: cache -> prefix bookkeeping -> speculation/base."""
        if self._cache is not None:
            outcome = await self._cache.lookup(request, namespace=self._namespace)
            if outcome.result is not None:
                return outcome.result

        if self._prefix_book is not None:
            self._prefix_book.plan_and_register(word_tokens(request.prompt_text))

        result = await self._compute(request)

        if self._cache is not None:
            await self._cache.store(request, result, namespace=self._namespace)
        return result

    async def _compute(self, request: GenerationRequest) -> GenerationResult:
        if self._speculative is not None:
            out = await self._speculative.decode(request)
            return out.result.with_meta(cache="miss")
        result = await self._base.generate(request)
        return result.with_meta(cache="miss")

    # -- orthogonal entry points ------------------------------------------ #

    async def race(
        self,
        request: GenerationRequest,
        candidates: Sequence[ProviderCandidate],
        *,
        cost_cap: float = float("inf"),
        hedge_delay: float = 0.0,
        validate: Validator | None = None,
    ) -> FanOutResult:
        """Run a first-good-wins fan-out across ``candidates`` for ``request``."""
        racer = FanOutRacer(
            cost_cap=cost_cap,
            hedge_delay=hedge_delay,
            validate=validate,
            clock=self._clock,
        )
        return await racer.race(request, candidates)

    async def generate_constrained(
        self,
        request: GenerationRequest,
        constraint: Constraint,
        *,
        max_repairs: int = 2,
        use_cache: bool = True,
    ) -> ConstrainedResult:
        """Generate output satisfying ``constraint`` (with bounded repair).

        Uses this gateway's accelerated :meth:`generate` as the underlying
        generate function (so cache + speculation still apply) unless
        ``use_cache`` is False, in which case the base backend is used raw.
        """
        gen = self.generate if use_cache else self._base.generate
        decoder = ConstrainedDecoder(gen, max_repairs=max_repairs)
        return await decoder.decode(request, constraint)


__all__ = ["AcceleratedGateway"]
