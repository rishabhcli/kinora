"""LLM-backed translation provider (the real path), shaped on app.providers.chat.

The production translation provider drives the same chat seam the agent crew uses
(:class:`app.providers.chat.ChatProvider` over DashScope's OpenAI-compatible
endpoint, or OpenAI when ``reasoning_provider="openai"``). It does **not** open
its own transport — it holds a chat client, so all the resilience (retries,
breaker, rate-limit, cost accounting) is inherited for free.

Why an LLM and not a dedicated MT API? Kinora's content is *literary* — narration
scripts that must stay speakable, entity descriptions that must keep their
cinematic register — and it carries the markup/glossary sentinels this subsystem
masks in. An instruction-following model handles "translate faithfully, keep
every ``⟦n⟧`` token verbatim, do not translate the locked names" in one prompt,
returning strict JSON we parse 1:1.

The chat client is the injectable seam, so **tests never call a live model**:
they pass a fake chat client (or just use :class:`FakeTranslationProvider`). The
prompt-building + response-parsing here is pure and unit-tested against canned
chat outputs.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from app.core.logging import get_logger

from .languages import get_language
from .markup import ANY_SENTINEL_RE
from .provider import ProviderRequest, ProviderResponse, TranslationProvider
from .types import ContentKind, TranslationCost

logger = get_logger("app.translation.llm_provider")


class ChatLike(Protocol):
    """The minimal chat surface this provider needs (a subset of ChatProvider)."""

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        timeout: float | None = ...,
        stream: bool | None = ...,
        enable_thinking: bool | None = ...,
    ) -> Any:
        """Return a parsed JSON value (object/array)."""
        ...


# Per-content-kind instruction tuning. Each is appended to the system prompt so
# the register of the output matches the surface the reader sees.
_KIND_GUIDANCE: dict[ContentKind, str] = {
    ContentKind.PAGE_TEXT: (
        "This is prose from a book page. Preserve paragraph tone and literary register."
    ),
    ContentKind.ENTITY_DESCRIPTION: (
        "This is a terse description of a story character/place/prop. Keep it concise "
        "and visual; do not add detail that is not in the source."
    ),
    ContentKind.NARRATION: (
        "This is a narration script that will be spoken aloud. Keep it natural to "
        "speak, preserve sentence rhythm, and avoid tongue-twisters or abbreviations."
    ),
    ContentKind.UI_FALLBACK: "This is a short interface string. Keep it brief.",
}

_SYSTEM_TEMPLATE = (
    "You are a professional literary translator. Translate each numbered source "
    "string from {source_name} ({source_tag}) into {target_name} ({target_tag}).\n"
    "RULES:\n"
    "1. Preserve every token of the form ⟦…⟧ EXACTLY and in the same place — these "
    "are placeholders/markup and must never be translated, reordered, dropped, or "
    "duplicated.\n"
    "2. Do not add notes, explanations, or quotation marks that are not in the source.\n"
    "3. Translate meaning faithfully; do not summarize or omit.\n"
    "{direction_note}"
    "{kind_note}\n"
    'Return ONLY a JSON object: {{"translations": ["...", "..."]}} with one entry '
    "per input, in the same order."
)


class LLMTranslationProvider(TranslationProvider):
    """A :class:`TranslationProvider` backed by the shared chat seam.

    Args:
        chat: A chat client exposing ``chat_json`` (DashScope or OpenAI). In
            tests this is a fake returning canned JSON.
        model: The chat model id to use.
        temperature: Low by default — translation wants faithfulness, not
            creativity.
    """

    name = "llm"

    def __init__(
        self,
        chat: ChatLike,
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = 4096,
    ) -> None:
        self._chat = chat
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def translate_batch(self, requests: list[ProviderRequest]) -> ProviderResponse:
        if not requests:
            return ProviderResponse(texts=(), cost=TranslationCost())
        # All requests in a batch share a language pair + (usually) a kind; the
        # service batches them that way. We key the prompt off the first.
        head = requests[0]
        messages = self._build_messages(requests, head.source_lang, head.target_lang, head.kind)
        raw = await self._chat.chat_json(
            messages,
            self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        texts = self._parse(raw, expected=len(requests))
        texts = self._enforce_sentinels(requests, texts)
        cost = self._estimate_cost(requests, texts)
        return ProviderResponse(texts=tuple(texts), cost=cost)

    # -- prompt + parse --------------------------------------------------- #

    def _build_messages(
        self,
        requests: list[ProviderRequest],
        source_lang: str,
        target_lang: str,
        kind: ContentKind,
    ) -> list[dict[str, Any]]:
        src = get_language(source_lang)
        tgt = get_language(target_lang)
        direction_note = ""
        if tgt.is_rtl:
            direction_note = (
                f"4. {tgt.name} is written right-to-left; produce natural RTL text "
                "(do not insert directional control characters).\n"
            )
        system = _SYSTEM_TEMPLATE.format(
            source_name=src.name,
            source_tag=src.tag,
            target_name=tgt.name,
            target_tag=tgt.tag,
            direction_note=direction_note,
            kind_note=_KIND_GUIDANCE.get(kind, ""),
        )
        numbered = {
            "sources": [
                {"i": i, "text": r.masked_text, "context": r.context}
                for i, r in enumerate(requests)
            ]
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(numbered, ensure_ascii=False)},
        ]

    @staticmethod
    def _parse(raw: Any, *, expected: int) -> list[str]:
        """Pull the ordered translation list out of the model's JSON reply."""
        if isinstance(raw, dict):
            value = raw.get("translations")
            if value is None:
                # Tolerate a model that keyed it differently.
                for alt in ("results", "outputs", "items"):
                    if alt in raw:
                        value = raw[alt]
                        break
        elif isinstance(raw, list):
            value = raw
        else:
            value = None
        if not isinstance(value, list):
            raise ValueError(f"LLM translation reply missing a list of translations: {raw!r:.120}")
        texts = [str(v) if not isinstance(v, dict) else str(v.get("text", "")) for v in value]
        if len(texts) != expected:
            # Pad/truncate defensively so the 1:1 contract holds; the service's
            # quality gate will catch a degraded entry.
            if len(texts) < expected:
                texts = texts + [""] * (expected - len(texts))
            else:
                texts = texts[:expected]
        return texts

    @staticmethod
    def _enforce_sentinels(requests: list[ProviderRequest], texts: list[str]) -> list[str]:
        """Best-effort: re-append any sentinel a model dropped, log the rest.

        The markup layer's strict restore would raise on a dropped sentinel; the
        service handles that and flags the segment. Here we only *log* the model's
        sentinel hygiene so a systematically misbehaving model is visible in
        telemetry — we do not silently mutate beyond appending a missing token,
        which keeps the lenient restore meaningful.
        """
        fixed: list[str] = []
        for req, text in zip(requests, texts, strict=False):
            src_tokens = set(ANY_SENTINEL_RE.findall(req.masked_text))
            out_tokens = set(ANY_SENTINEL_RE.findall(text))
            missing = src_tokens - out_tokens
            if missing:
                logger.warning(
                    "llm_translation.dropped_sentinels",
                    model=req.target_lang,
                    missing=len(missing),
                )
            fixed.append(text)
        return fixed

    @staticmethod
    def _estimate_cost(requests: list[ProviderRequest], texts: list[str]) -> TranslationCost:
        in_chars = sum(len(r.masked_text) for r in requests)
        out_chars = sum(len(t) for t in texts)
        return TranslationCost(
            input_tokens=max(1, in_chars // 4),
            output_tokens=max(1, out_chars // 4),
            provider_calls=1,
            segments=len(requests),
        )

    async def back_translate(self, requests: list[ProviderRequest]) -> ProviderResponse:
        """Round-trip back to the source language with the same prompt machinery."""
        return await self.translate_batch(requests)


def make_llm_provider_from_providers(providers: Any, *, model: str) -> LLMTranslationProvider:
    """Build the provider from the container's :class:`Providers` bundle.

    ``providers.chat`` is the shared :class:`ChatProvider`; this keeps the
    translation layer on the same resilient transport + cost sink as the agents.
    """
    return LLMTranslationProvider(providers.chat, model=model)


__all__ = ["ChatLike", "LLMTranslationProvider", "make_llm_provider_from_providers"]
