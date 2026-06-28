"""Tests for the LLM-backed provider using a fake chat client (no live calls)."""

from __future__ import annotations

from typing import Any

import pytest

from app.translation.llm_provider import LLMTranslationProvider
from app.translation.memory_store import TranslationMemory
from app.translation.provider import ProviderRequest
from app.translation.service import TranslationService
from app.translation.types import ContentKind, Segment, TranslationRequest

pytestmark = pytest.mark.asyncio


class FakeChat:
    """A fake chat client returning canned JSON; records the prompts it saw."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat_json(self, messages: list[dict[str, Any]], model: str, **kw: Any) -> Any:
        self.calls.append(messages)
        return self._responses.pop(0)


async def test_parses_translations_list() -> None:
    chat = FakeChat([{"translations": ["Bonjour", "Au revoir"]}])
    provider = LLMTranslationProvider(chat, model="m")
    resp = await provider.translate_batch(
        [
            ProviderRequest("Hello", "en", "fr"),
            ProviderRequest("Goodbye", "en", "fr"),
        ]
    )
    assert resp.texts == ("Bonjour", "Au revoir")
    assert resp.cost.provider_calls == 1
    assert resp.cost.segments == 2


async def test_system_prompt_mentions_languages_and_sentinels() -> None:
    chat = FakeChat([{"translations": ["x"]}])
    provider = LLMTranslationProvider(chat, model="m")
    await provider.translate_batch([ProviderRequest("Hi ⟦0⟧", "en", "ar")])
    system = chat.calls[0][0]["content"]
    assert "English" in system and "Arabic" in system
    assert "⟦" in system  # the sentinel-preservation rule
    assert "right-to-left" in system  # RTL note for an RTL target


async def test_pads_short_reply_to_expected_count() -> None:
    chat = FakeChat([{"translations": ["only one"]}])
    provider = LLMTranslationProvider(chat, model="m")
    resp = await provider.translate_batch(
        [ProviderRequest("a", "en", "fr"), ProviderRequest("b", "en", "fr")]
    )
    assert len(resp.texts) == 2  # padded


async def test_tolerates_alternate_key() -> None:
    chat = FakeChat([{"results": ["Hola"]}])
    provider = LLMTranslationProvider(chat, model="m")
    resp = await provider.translate_batch([ProviderRequest("Hello", "en", "es")])
    assert resp.texts == ("Hola",)


async def test_tolerates_bare_list_reply() -> None:
    chat = FakeChat([["Ciao"]])
    provider = LLMTranslationProvider(chat, model="m")
    resp = await provider.translate_batch([ProviderRequest("Hello", "en", "it")])
    assert resp.texts == ("Ciao",)


async def test_raises_on_unparseable_reply() -> None:
    chat = FakeChat([{"unexpected": "shape"}])
    provider = LLMTranslationProvider(chat, model="m")
    with pytest.raises(ValueError):
        await provider.translate_batch([ProviderRequest("Hello", "en", "fr")])


async def test_empty_batch_short_circuits() -> None:
    chat = FakeChat([])
    provider = LLMTranslationProvider(chat, model="m")
    resp = await provider.translate_batch([])
    assert resp.texts == ()
    assert chat.calls == []


async def test_service_with_llm_provider_end_to_end() -> None:
    # Wire the LLM provider into the service; the fake chat preserves the sentinel.
    chat = FakeChat([{"translations": ["Bonjour ⟦0⟧ monde"]}])
    provider = LLMTranslationProvider(chat, model="m")
    svc = TranslationService(provider, memory=TranslationMemory())
    seg = Segment(id="s0", text="Hello {name} world", kind=ContentKind.PAGE_TEXT)
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="fr", segments=(seg,), source_lang="en")
    )
    # The masked {name} sentinel survived and was restored.
    assert "{name}" in res.segments[0].translated_text
