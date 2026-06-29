"""Adapters bridging the production transport to the accel protocols.

The acceleration layer is written against the small
:class:`~app.inference.accel.protocol.InferenceBackend` / ``Embedder`` /
``TokenScorer`` protocols so it never hard-imports the DashScope transport. These
adapters are the *seam*: production code constructs them with the concrete
``ChatProvider`` / ``EmbeddingProvider`` and hands the result to an
:class:`~app.inference.accel.gateway.AcceleratedGateway`.

Imports of the provider layer are kept local to the methods (lazy) so importing
this module — or the package — never drags in ``httpx`` / ``dashscope``; the
adapters only touch the transport when actually called.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .protocol import GenerationRequest, GenerationResult
from .tokenize import word_tokens


@runtime_checkable
class ChatLike(Protocol):
    """The slice of ``app.providers.chat.ChatProvider`` the adapter calls."""

    async def chat(self, messages: Any, model: str, **kwargs: Any) -> Any: ...


@runtime_checkable
class EmbedLike(Protocol):
    """The slice of ``app.providers.embeddings.EmbeddingProvider`` the adapter calls."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class ChatBackend:
    """Adapts a chat provider to the :class:`InferenceBackend` protocol.

    ``model`` overrides the request's logical model with the concrete provider
    model id; if ``None`` the request's ``model`` is passed through.
    """

    def __init__(self, chat: ChatLike, *, model: str | None = None) -> None:
        self._chat = chat
        self._model = model

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        messages = [{"role": role, "content": content} for role, content in request.messages]
        model = self._model or request.model
        result = await self._chat.chat(
            messages,
            model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        text = getattr(result, "text", "") or ""
        return GenerationResult(
            text=text,
            tokens=word_tokens(text),
            model=getattr(result, "model", model),
            finish_reason=getattr(result, "finish_reason", None) or "stop",
            input_tokens=getattr(result, "input_tokens", 0) or 0,
            output_tokens=getattr(result, "output_tokens", 0) or 0,
            meta={"backend": "chat_provider"},
        )


class EmbeddingAdapter:
    """Adapts a text-embedding provider to the :class:`Embedder` protocol."""

    def __init__(self, embeddings: EmbedLike) -> None:
        self._embeddings = embeddings

    async def embed(self, text: str) -> tuple[float, ...]:
        vectors = await self._embeddings.embed_texts([text])
        if not vectors:
            return ()
        return tuple(vectors[0])


__all__ = ["ChatBackend", "ChatLike", "EmbedLike", "EmbeddingAdapter"]
