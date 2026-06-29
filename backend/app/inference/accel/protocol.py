"""The inference contract the accel layer consumes — request/response types and
the :class:`InferenceBackend` / :class:`TokenScorer` protocols.

Facet A (the gateway transport) is expected to expose an ``InferenceBackend``;
this module defines the *shape* the acceleration facet depends on so the two
compose without a hard import. If facet A's protocol differs, a thin adapter in
``app.inference`` can bridge it — the accel layer only ever talks to the names
defined here.

Design rules:

* Everything is a frozen, hashable-where-possible value object. A
  :class:`GenerationRequest` is the cache key material for the semantic cache and
  the prefix-reuse trie, so it must be cheap to fingerprint.
* The token vocabulary is *string tokens* (words / sub-words as opaque strings).
  Real tokenizers map to ints; the accel algorithms (speculative accept/reject,
  prefix LCP, constrained masking) are vocabulary-agnostic and work on whatever
  unit the backend emits. The deterministic test backends emit whitespace words.
* No method here performs a network call by contract — an implementation may,
  but the accel orchestrators are written so that the *number* and *content* of
  backend calls is fully determined by their inputs (the property the tests
  pin down).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A normalized request for text generation.

    The prompt is a list of role/content message dicts (OpenAI-compatible), kept
    as a tuple-of-tuples internally so the request is hashable and a stable cache
    key. Use :meth:`from_messages` to build one from the mutable list shape the
    provider layer uses.
    """

    #: Frozen messages: a tuple of (role, content) pairs.
    messages: tuple[tuple[str, str], ...]
    #: Logical model id (a routing label, not necessarily a provider model id).
    model: str = "default"
    temperature: float = 0.0
    max_tokens: int = 256
    #: Opaque tag bag (e.g. ``{"agent": "adapter"}``) — namespacing + telemetry.
    #: Excluded from the cache fingerprint unless explicitly added to it.
    tags: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str = "default",
        temperature: float = 0.0,
        max_tokens: int = 256,
        tags: Mapping[str, str] | None = None,
    ) -> GenerationRequest:
        """Build a request from the mutable ``[{"role":..,"content":..}]`` shape."""
        frozen = tuple((str(m["role"]), str(m["content"])) for m in messages)
        frozen_tags = tuple(sorted((str(k), str(v)) for k, v in (tags or {}).items()))
        return cls(
            messages=frozen,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tags=frozen_tags,
        )

    @classmethod
    def from_prompt(
        cls, prompt: str, *, model: str = "default", **kwargs: object
    ) -> GenerationRequest:
        """Convenience: a single user-role message from a bare prompt string."""
        return cls.from_messages([{"role": "user", "content": prompt}], model=model, **kwargs)  # type: ignore[arg-type]

    @property
    def prompt_text(self) -> str:
        """The concatenated message contents — the unit the semantic cache embeds."""
        return "\n".join(content for _role, content in self.messages)

    def with_max_tokens(self, max_tokens: int) -> GenerationRequest:
        return replace(self, max_tokens=max_tokens)

    def with_model(self, model: str) -> GenerationRequest:
        return replace(self, model=model)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The output of a (possibly accelerated) generation.

    ``tokens`` is the ordered list of emitted string tokens; ``text`` is their
    joined rendering. ``meta`` records how the answer was produced so callers and
    the metrics layer can attribute savings (cache hit, speculative acceptance,
    which provider won a race, etc.).
    """

    text: str
    tokens: tuple[str, ...] = ()
    model: str = "default"
    finish_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0
    meta: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_tokens(
        cls,
        tokens: Sequence[str],
        *,
        model: str = "default",
        finish_reason: str = "stop",
        input_tokens: int = 0,
        joiner: str = " ",
        meta: Mapping[str, object] | None = None,
    ) -> GenerationResult:
        toks = tuple(tokens)
        return cls(
            text=joiner.join(toks),
            tokens=toks,
            model=model,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=len(toks),
            meta=dict(meta or {}),
        )

    def with_meta(self, **extra: object) -> GenerationResult:
        merged = {**self.meta, **extra}
        return replace(self, meta=merged)


@dataclass(frozen=True, slots=True)
class TokenProposal:
    """A draft model's proposed continuation for speculative decoding.

    ``tokens`` are the proposed next tokens; the orchestrator asks the target to
    verify them. ``confidences`` (optional, same length) lets an adaptive
    controller weigh how aggressively to trust the draft.
    """

    tokens: tuple[str, ...]
    confidences: tuple[float, ...] = ()


@runtime_checkable
class InferenceBackend(Protocol):
    """The minimal text-generation contract the accel layer wraps.

    A backend turns a :class:`GenerationRequest` into a :class:`GenerationResult`.
    This is the sibling-facet protocol; the production implementation adapts the
    DashScope ``ChatProvider``. Acceleration components compose one or more of
    these.
    """

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Produce a full generation for ``request``."""
        ...


@runtime_checkable
class DraftBackend(Protocol):
    """A fast, cheap backend that *proposes* a continuation given prefix tokens.

    Used as the draft model in speculative decoding. Implementations are
    typically a small model; the orchestrator never trusts a proposal without
    target verification, so a draft may be arbitrarily wrong without affecting
    correctness — only speed.
    """

    async def propose(
        self, request: GenerationRequest, committed: tuple[str, ...], k: int
    ) -> TokenProposal:
        """Propose up to ``k`` next tokens following ``committed`` for ``request``."""
        ...


@runtime_checkable
class TokenScorer(Protocol):
    """A target backend that can score / verify candidate next tokens.

    Speculative decoding needs the target to answer, for a given prefix, *what
    token would I emit next?* — and to do so for the whole proposed window in one
    verification pass. :meth:`verify` returns the target's own next token for each
    position ``committed + proposal[:i]``; the orchestrator accepts the longest
    matching prefix (see :mod:`app.inference.accel.speculative`).
    """

    async def verify(
        self,
        request: GenerationRequest,
        committed: tuple[str, ...],
        proposal: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Return the target's next token at each of ``len(proposal)+1`` positions.

        Position ``i`` (``0 <= i <= len(proposal)``) is the target's next token
        given the prefix ``committed + proposal[:i]``. The extra trailing element
        is the *correction / bonus* token used when the whole proposal is
        accepted. An empty string at a position signals the target would stop
        there.
        """
        ...

    async def is_finished(self, request: GenerationRequest, committed: tuple[str, ...]) -> bool:
        """Whether the target would stop generating after ``committed``."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Embeds text into a fixed-dimension vector for the semantic cache."""

    async def embed(self, text: str) -> tuple[float, ...]:
        """Return a (typically unit-normalized) embedding for ``text``."""
        ...


__all__ = [
    "DraftBackend",
    "Embedder",
    "GenerationRequest",
    "GenerationResult",
    "InferenceBackend",
    "TokenProposal",
    "TokenScorer",
]
