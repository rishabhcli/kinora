"""The answer synthesizer — turn assembled context into a grounded answer.

This is the only layer that touches the chat provider, and it does so behind a
tiny :class:`ChatClient` protocol (the slice of :class:`app.providers.chat.\
ChatProvider` we need), so the real provider *and* a deterministic fake both fit
without inheritance — tests never make a live call (zero credits).

Two paths:

* :meth:`synthesize` — one ``chat_json`` round-trip asking for the strict JSON
  answer contract (prose + structured citations). The model's draft is then run
  through the :class:`~app.assistant.grounding.GroundingGuard`, which validates
  the citations against the assembled context independent of the model.
* :meth:`stream` — token-by-token over the chat seam's SSE path for the live UI;
  it streams plain text deltas, then parses + guards the assembled draft at the
  end so the final :class:`Answer` is just as grounded as the non-streaming one.

If the model returns nothing usable (empty / unparseable / refusal), the
synthesizer returns a grounded *refusal* rather than fabricating — the §8 read
side must fail closed, never hallucinate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.assistant.context import AssembledContext
from app.assistant.grounding import GroundingGuard
from app.assistant.prompts import REFUSAL_SENTINEL, build_messages
from app.assistant.types import (
    Answer,
    AssistantIntent,
    ConversationTurn,
    StreamDelta,
)
from app.core.logging import get_logger
from app.providers.chat import extract_json
from app.providers.types import ChatResult

logger = get_logger("app.assistant.synth")

#: Conservative default cap for an answer (keeps replies tight + cheap).
DEFAULT_MAX_TOKENS = 700


class ChatClient(Protocol):
    """The slice of the chat provider the synthesizer depends on.

    Both methods are async; ``chat_json`` returns parsed JSON, ``chat`` returns a
    :class:`ChatResult` (and, with ``stream=True``, the provider streams under the
    hood — here we re-implement the streamed UX over ``chat`` so the seam stays a
    two-method protocol a fake can satisfy trivially).
    """

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        stream: bool | None = ...,
    ) -> dict[str, Any] | list[Any]: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        stream: bool | None = ...,
    ) -> ChatResult: ...


class AnswerSynthesizer:
    """Produce a grounded :class:`Answer` from context via the chat seam."""

    def __init__(
        self,
        chat: ChatClient,
        model: str,
        *,
        guard: GroundingGuard | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.2,
    ) -> None:
        self._chat = chat
        self._model = model
        self._guard = guard or GroundingGuard()
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def synthesize(
        self,
        question: str,
        context: AssembledContext,
        *,
        intent: AssistantIntent = AssistantIntent.GENERAL,
        history: list[ConversationTurn] | None = None,
    ) -> Answer:
        """One round-trip: ask for the JSON contract, then guard the draft.

        Short-circuits to a grounded refusal when there is no context at all —
        no point spending a call when nothing was retrieved.
        """
        if context.is_empty:
            return _refusal()

        messages = build_messages(
            question, context, intent=intent, history=history, require_json=True
        )
        try:
            raw = await self._chat.chat_json(
                messages,
                self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001 - never 500 a reader's question
            logger.warning("assistant.synth_failed", error=str(exc))
            return _refusal()

        draft, declared = _parse_contract(raw)
        if not draft:
            return _refusal()
        return self._guard.verify(draft, context, declared_markers=declared)

    async def stream(
        self,
        question: str,
        context: AssembledContext,
        *,
        intent: AssistantIntent = AssistantIntent.GENERAL,
        history: list[ConversationTurn] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream the answer as text deltas, then a final guarded :class:`Answer`.

        The model is asked for *plain prose with inline markers* (not JSON) so the
        stream reads naturally to the UI; the assembled prose is then parsed +
        guarded at the end so the terminal ``done`` delta carries a fully grounded
        answer with validated citations.
        """
        if context.is_empty:
            refusal = _refusal()
            yield StreamDelta(type="token", text=refusal.text)
            yield StreamDelta(type="done", answer=refusal)
            return

        messages = build_messages(
            question, context, intent=intent, history=history, require_json=False
        )
        chunks: list[str] = []
        try:
            result = await self._chat.chat(
                messages,
                self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                stream=True,
            )
            text = result.text or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant.stream_failed", error=str(exc))
            refusal = _refusal()
            yield StreamDelta(type="token", text=refusal.text)
            yield StreamDelta(type="done", answer=refusal)
            return

        # Emit the prose in sentence-ish chunks so the UI animates without us
        # holding a real socket (the provider already streamed it server-side).
        for piece in _chunk_for_stream(text):
            chunks.append(piece)
            yield StreamDelta(type="token", text=piece)

        answer = self._guard.verify("".join(chunks), context)
        yield StreamDelta(type="citations", citations=answer.citations)
        yield StreamDelta(type="done", answer=answer)


def _parse_contract(raw: dict[str, Any] | list[Any]) -> tuple[str, list[int]]:
    """Extract ``(answer_text, declared_markers)`` from the JSON contract.

    Tolerant: accepts the documented object, a bare string, or a list of
    citation ints; anything unrecognized yields an empty draft (→ refusal).
    """
    if isinstance(raw, str):
        return raw.strip(), []
    if isinstance(raw, dict):
        if raw.get("refused") is True:
            return "", []
        answer = str(raw.get("answer") or "").strip()
        declared: list[int] = []
        for c in raw.get("citations") or []:
            try:
                declared.append(int(c))
            except (TypeError, ValueError):
                continue
        return answer, declared
    return "", []


def _chunk_for_stream(text: str, *, size: int = 24) -> list[str]:
    """Split a finished answer into small word-aligned chunks for streaming UX."""
    words = text.split(" ")
    out: list[str] = []
    buf: list[str] = []
    count = 0
    for w in words:
        buf.append(w)
        count += len(w) + 1
        if count >= size:
            out.append(" ".join(buf) + " ")
            buf, count = [], 0
    if buf:
        out.append(" ".join(buf))
    return out


def _refusal() -> Answer:
    return Answer(text=REFUSAL_SENTINEL, grounded=False, refused=True, citation_coverage=0.0)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "AnswerSynthesizer",
    "ChatClient",
    "extract_json",
]
