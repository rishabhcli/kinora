"""Deterministic, network-free backends for testing the accel layer.

These are first-class members of the package (not test-only) so other
subsystems and the integration tests can wire a fully simulated inference
gateway with zero live calls and zero credits — the project-wide constraint
(``KINORA_LIVE_VIDEO`` off, no DashScope key needed).

The headline guarantees:

* :class:`ScriptedTarget` is a deterministic oracle: it maps a request to a
  fixed output token sequence. As both an :class:`InferenceBackend` (full
  ``generate``) **and** a :class:`TokenScorer` (``verify``), it lets a test
  assert that speculative decoding reproduces ``generate`` byte-for-byte.
* :class:`ScriptedDraft` proposes tokens that may match the target perfectly,
  partially, or never — so the acceptance-rate math is exercised across the
  whole range without randomness.
* :class:`HashEmbedder` embeds text into a unit vector deterministically, with a
  knob to force two prompts to be near-duplicates (for the semantic cache).
* :class:`CountingBackend` wraps any backend and counts calls — the cache and
  prefix-reuse tests assert "the underlying model was called exactly once".
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Callable, Mapping, Sequence

from .protocol import (
    GenerationRequest,
    GenerationResult,
    TokenProposal,
)


class ScriptedTarget:
    """A deterministic target model: request -> fixed continuation tokens.

    The continuation is resolved by ``script`` (a mapping from a request key to
    a token list) or, if the key is absent, by ``default_fn(request)``. The
    *same* token sequence backs both :meth:`generate` and :meth:`verify`, which
    is exactly what makes "speculative == non-speculative" provable.
    """

    def __init__(
        self,
        script: Mapping[str, Sequence[str]] | None = None,
        *,
        default_fn: Callable[[GenerationRequest], Sequence[str]] | None = None,
        model: str = "target",
        key_fn: Callable[[GenerationRequest], str] | None = None,
    ) -> None:
        self._script = {k: tuple(v) for k, v in (script or {}).items()}
        self._default_fn = default_fn
        self.model = model
        self._key_fn = key_fn or (lambda r: r.prompt_text)
        self.generate_calls = 0
        self.verify_calls = 0

    def _full_output(self, request: GenerationRequest) -> tuple[str, ...]:
        key = self._key_fn(request)
        if key in self._script:
            seq = self._script[key]
        elif self._default_fn is not None:
            seq = tuple(self._default_fn(request))
        else:
            seq = ()
        return tuple(seq)[: request.max_tokens]

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.generate_calls += 1
        tokens = self._full_output(request)
        return GenerationResult.from_tokens(
            tokens,
            model=self.model,
            input_tokens=len(request.prompt_text.split()),
            meta={"backend": "scripted_target"},
        )

    # -- TokenScorer ----------------------------------------------------- #

    async def verify(
        self,
        request: GenerationRequest,
        committed: tuple[str, ...],
        proposal: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Target's own next token at each prefix position ``committed+proposal[:i]``.

        Because the target is an oracle, its next token after any *correct*
        prefix of length ``L`` is simply ``full_output[L]`` (or ``""`` past the
        end). Crucially this does NOT consult the proposal's content — the target
        only knows its own sequence — which is what guarantees correctness even
        when the draft proposes garbage.
        """
        self.verify_calls += 1
        full = self._full_output(request)
        base = len(committed)
        out: list[str] = []
        for i in range(len(proposal) + 1):
            idx = base + i
            out.append(full[idx] if idx < len(full) else "")
        return tuple(out)

    async def is_finished(
        self, request: GenerationRequest, committed: tuple[str, ...]
    ) -> bool:
        return len(committed) >= len(self._full_output(request))


class ScriptedDraft:
    """A deterministic draft model.

    ``proposer(request, committed, k)`` returns the proposed tokens (truncated to
    ``k``). The default proposer reads from a ``oracle`` (typically the same
    script as the target) but can be perturbed: ``corrupt_at`` injects a wrong
    token at a fixed offset to drive partial acceptance, and ``stop_after`` caps
    how many correct tokens the draft will ever propose.
    """

    def __init__(
        self,
        *,
        oracle: Callable[[GenerationRequest], Sequence[str]],
        corrupt_at: int | None = None,
        stop_after: int | None = None,
        confidence: float = 0.9,
    ) -> None:
        self._oracle = oracle
        self._corrupt_at = corrupt_at
        self._stop_after = stop_after
        self._confidence = confidence
        self.propose_calls = 0

    async def propose(
        self, request: GenerationRequest, committed: tuple[str, ...], k: int
    ) -> TokenProposal:
        self.propose_calls += 1
        full = tuple(self._oracle(request))
        base = len(committed)
        proposed: list[str] = []
        for i in range(k):
            idx = base + i
            if idx >= len(full):
                break
            if self._stop_after is not None and i >= self._stop_after:
                break
            token = full[idx]
            if self._corrupt_at is not None and idx == self._corrupt_at:
                token = f"__WRONG__{token}"
            proposed.append(token)
        confs = tuple(self._confidence for _ in proposed)
        return TokenProposal(tokens=tuple(proposed), confidences=confs)


class HashEmbedder:
    """Deterministic text embedder for the semantic cache tests.

    Maps text to a fixed-dim unit vector via a salted hash. A ``alias`` map lets
    a test declare that two distinct prompt strings should embed *identically*
    (a perfect near-duplicate), and ``perturb`` adds a tiny controllable angle so
    near-duplicates land just inside/outside a cosine threshold.
    """

    def __init__(
        self,
        *,
        dim: int = 16,
        alias: Mapping[str, str] | None = None,
    ) -> None:
        self.dim = dim
        self._alias = dict(alias or {})
        self.embed_calls = 0

    def _vector(self, text: str) -> tuple[float, ...]:
        canonical = self._alias.get(text, text)
        out: list[float] = []
        for i in range(self.dim):
            h = hashlib.sha256(f"{i}:{canonical}".encode()).digest()
            # Two bytes -> a value in [-1, 1].
            v = int.from_bytes(h[:2], "big") / 65535.0 * 2.0 - 1.0
            out.append(v)
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return tuple(x / norm for x in out)

    async def embed(self, text: str) -> tuple[float, ...]:
        self.embed_calls += 1
        return self._vector(text)


class CountingBackend:
    """Wraps an :class:`InferenceBackend`-like object, counting ``generate`` calls."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0
        self.seen: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        self.seen.append(request)
        return await self._inner.generate(request)  # type: ignore[attr-defined]


class StaticBackend:
    """A backend that always returns the same text (handy for fan-out tests)."""

    def __init__(
        self,
        text: str,
        *,
        model: str = "static",
        fail_with: BaseException | None = None,
    ) -> None:
        self._text = text
        self.model = model
        self._fail_with = fail_with

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self._fail_with is not None:
            raise self._fail_with
        from .tokenize import word_tokens

        return GenerationResult.from_tokens(
            word_tokens(self._text), model=self.model, meta={"backend": "static"}
        )


class GatedBackend:
    """A backend whose ``generate`` blocks on an explicit gate until released.

    Lets a fan-out test order candidate completions deterministically: a test
    releases the gate of whichever provider it wants to "win", with no reliance
    on wall-clock timing. An optional ``fail_with`` makes the gated candidate
    raise after release (to test failure fall-through).
    """

    def __init__(
        self,
        text: str,
        *,
        model: str = "gated",
        fail_with: BaseException | None = None,
    ) -> None:
        self._text = text
        self.model = model
        self._fail_with = fail_with
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls = 0

    def release(self) -> None:
        self.gate.set()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        self.started.set()
        await self.gate.wait()
        if self._fail_with is not None:
            raise self._fail_with
        from .tokenize import word_tokens

        return GenerationResult.from_tokens(
            word_tokens(self._text), model=self.model, meta={"backend": "gated"}
        )


class ManualClock:
    """A clock whose ``sleep`` resolves only when the cancel event fires.

    Wired into :class:`~app.inference.accel.fanout.FanOutRacer`, this makes the
    hedge delay *event-driven*: the racer's wait for ``hedge_delay`` returns the
    instant a winner is signalled, and otherwise blocks (the test controls when
    the next candidate launches by releasing gates). No real time elapses.
    """

    __slots__ = ("_mono", "_wall")

    def __init__(self, *, start: float = 1_700_000_000.0) -> None:
        self._wall = float(start)
        self._mono = 0.0

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += seconds

    async def sleep(self, seconds: float, cancel: asyncio.Event) -> None:
        """Block until ``cancel`` is set (the hedge never times out on its own)."""
        if seconds <= 0:
            return
        await cancel.wait()


__all__ = [
    "CountingBackend",
    "GatedBackend",
    "HashEmbedder",
    "ManualClock",
    "ScriptedDraft",
    "ScriptedTarget",
    "StaticBackend",
]
