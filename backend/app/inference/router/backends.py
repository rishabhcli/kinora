"""Concrete :class:`InferenceBackend` implementations — the transport adapters.

The router is a scheduling brain over an abstract
:class:`~app.inference.router.protocols.InferenceBackend`; these are the real
backends it dispatches to. They **compose with** the existing provider +
resilience layer (``app/providers``) — they never edit it:

* :class:`ChatProviderBackend` wraps an ``app.providers.chat.ChatProvider`` so a
  micro-batch of router requests becomes a set of concurrent chat completions,
  each already funneled through the round-1 transport (timeouts, breaker, rate
  limit) and — when the caller wires it — the round-2 resilience gateway. The
  router adds *cross-request* scheduling on top of that *per-call* resilience.

* :class:`EchoBackend` is a deterministic, network-free backend (fixed token
  counts) for tests, the simulator, and ``KINORA_LIVE_VIDEO``-off local runs —
  the bundled default so wiring the router never spends a credit.

The prompt content the backend needs is carried opaquely on
``InferenceRequest.metadata`` (``messages`` + per-call kwargs) so the scheduling
layer stays content-free; a backend reads it only at execution time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .errors import BackendError
from .protocols import InferenceResult
from .request import InferenceRequest

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime provider import
    from app.providers.chat import ChatProvider


class EchoBackend:
    """A deterministic fake backend: echoes fixed token counts, never blocks.

    The default backend for tests / simulator / off-gate local runs. Produces one
    successful :class:`InferenceResult` per request with ``output_tokens`` capped
    by the request's ``max_output_tokens`` (so token accounting stays realistic)
    and zero network I/O.
    """

    def __init__(self, model: str, *, output_tokens: int = 64) -> None:
        if not model:
            raise BackendError("model must be non-empty")
        self._model = model
        self._output_tokens = output_tokens

    @property
    def model(self) -> str:
        return self._model

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        out: list[InferenceResult] = []
        for r in requests:
            cap = r.max_output_tokens or self._output_tokens
            out.append(
                InferenceResult(
                    request_id=r.request_id,
                    model=self._model,
                    output_tokens=min(self._output_tokens, cap),
                    prompt_tokens=r.prompt_tokens,
                )
            )
        return out


class ChatProviderBackend:
    """Bridges a micro-batch of router requests to ``ChatProvider.chat`` calls.

    Each request in the batch runs as its own concurrent chat completion (the
    serving engine's "continuous batch" is realised here as bounded concurrency
    over the provider). Per-request prompt + kwargs are read from
    ``request.metadata`` under the keys:

    * ``"messages"`` — the chat message list (required; missing → a per-request
      error result, never a whole-batch raise);
    * ``"temperature"`` / ``"max_tokens"`` / ``"timeout"`` / ``"enable_thinking"``
      — optional passthroughs to ``ChatProvider.chat``.

    A single request's provider failure is captured as an ``error`` on its
    result; the batch as a whole only raises :class:`BackendError` for a
    programming fault (so the router's per-request settlement stays intact).
    """

    def __init__(self, provider: ChatProvider, model: str) -> None:
        if not model:
            raise BackendError("model must be non-empty")
        self._provider = provider
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        results = await asyncio.gather(
            *(self._run_one(r) for r in requests), return_exceptions=True
        )
        out: list[InferenceResult] = []
        for req, res in zip(requests, results, strict=True):
            if isinstance(res, BaseException):
                out.append(
                    InferenceResult(
                        request_id=req.request_id,
                        model=self._model,
                        output_tokens=0,
                        error=str(res),
                    )
                )
            else:
                out.append(res)
        return out

    async def _run_one(self, req: InferenceRequest) -> InferenceResult:
        messages = req.metadata.get("messages")
        if not isinstance(messages, list):
            return InferenceResult(
                request_id=req.request_id,
                model=self._model,
                output_tokens=0,
                error="missing 'messages' in request metadata",
            )
        kwargs: dict[str, Any] = {}
        for key in ("temperature", "max_tokens", "timeout", "enable_thinking"):
            if key in req.metadata:
                kwargs[key] = req.metadata[key]
        result = await self._provider.chat(messages, self._model, **kwargs)
        return InferenceResult(
            request_id=req.request_id,
            model=self._model,
            output_tokens=result.output_tokens,
            prompt_tokens=result.input_tokens,
        )


__all__ = ["ChatProviderBackend", "EchoBackend"]
