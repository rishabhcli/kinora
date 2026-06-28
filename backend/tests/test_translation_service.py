"""End-to-end tests of TranslationService with the fake provider (no live calls)."""

from __future__ import annotations

import pytest

from app.translation.glossary import Glossary, GlossaryEntry
from app.translation.memory_store import TranslationMemory
from app.translation.provider import FakeTranslationProvider
from app.translation.service import TranslationService
from app.translation.types import (
    ContentKind,
    Segment,
    TranslationOrigin,
    TranslationRequest,
)

pytestmark = pytest.mark.asyncio


def _service(**kw: object) -> TranslationService:
    return TranslationService(FakeTranslationProvider(), memory=TranslationMemory(), **kw)  # type: ignore[arg-type]


async def test_basic_translation_preserves_markup() -> None:
    svc = _service()
    seg = Segment(id="s0", text="See <b>{name}</b> at https://x.io")
    res = await svc.translate(
        TranslationRequest(
            book_id="bk", target_lang="fr", segments=(seg,), source_lang="en"
        )
    )
    out = res.segments[0]
    assert out.origin is TranslationOrigin.PROVIDER
    assert "<b>{name}</b>" in out.translated_text
    assert "https://x.io" in out.translated_text
    assert not out.needs_review


async def test_passthrough_same_language() -> None:
    svc = _service()
    seg = Segment(id="s0", text="Hello")
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="en", segments=(seg,), source_lang="en")
    )
    assert res.segments[0].origin is TranslationOrigin.PASSTHROUGH
    assert res.segments[0].translated_text == "Hello"
    assert res.cost.provider_calls == 0


async def test_cache_hit_on_second_run() -> None:
    svc = _service()
    seg = Segment(id="s0", text="The cat sat.")
    req = TranslationRequest(book_id="bk", target_lang="fr", segments=(seg,), source_lang="en")
    first = await svc.translate(req)
    assert first.cost.cache_hits == 0
    second = await svc.translate(req)
    assert second.cost.cache_hits == 1
    assert second.cost.provider_calls == 0
    assert second.segments[0].origin is TranslationOrigin.MEMORY


async def test_glossary_dnt_and_forced_target() -> None:
    g = Glossary(
        [
            GlossaryEntry(source="Elsa", do_not_translate=True),
            GlossaryEntry(source="Snow Queen", targets={"fr": "Reine des neiges"}),
        ]
    )
    svc = TranslationService(FakeTranslationProvider(), glossary=g, memory=TranslationMemory())
    seg = Segment(id="s0", text="Elsa, the Snow Queen, smiled.")
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="fr", segments=(seg,), source_lang="en")
    )
    text = res.segments[0].translated_text
    assert "Elsa" in text  # DNT preserved
    assert "Reine des neiges" in text  # forced target


async def test_rtl_target_sets_flag_and_isolates() -> None:
    svc = _service()
    seg = Segment(id="s0", text="Hello Kinora 2024")
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="ar", segments=(seg,), source_lang="en")
    )
    assert res.rtl is True
    assert "⁨" in res.segments[0].translated_text  # FSI isolate present


async def test_source_language_auto_detected() -> None:
    svc = _service()
    seg = Segment(id="s0", text="le chat est sur la table dans la maison")
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="en", segments=(seg,))
    )
    # Detected French → English; it should not be a passthrough.
    assert res.source_lang == "fr"


async def test_markup_corruption_flags_review() -> None:
    # A provider that drops a placeholder → markup warning → needs_review.
    svc = TranslationService(
        FakeTranslationProvider(corrupt_markup=True), memory=TranslationMemory()
    )
    seg = Segment(id="s0", text="Hello {name} and {place}")
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="fr", segments=(seg,), source_lang="en")
    )
    out = res.segments[0]
    assert out.needs_review
    assert out.warnings


async def test_back_translation_runs_extra_batch() -> None:
    fake = FakeTranslationProvider()
    svc = TranslationService(fake, memory=TranslationMemory())
    seg = Segment(id="s0", text="The dog runs")
    res = await svc.translate(
        TranslationRequest(
            book_id="bk",
            target_lang="fr",
            segments=(seg,),
            source_lang="en",
            back_translate=True,
        )
    )
    # Forward + back = 2 provider calls.
    assert res.cost.provider_calls == 2


async def test_batch_preserves_order() -> None:
    svc = _service()
    segs = tuple(Segment(id=f"s{i}", text=f"sentence number {i}") for i in range(10))
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="fr", segments=segs, source_lang="en")
    )
    assert [s.id for s in res.segments] == [f"s{i}" for i in range(10)]


async def test_translate_text_convenience() -> None:
    svc = _service()
    out = await svc.translate_text(
        "Hello world", book_id="bk", target_lang="fr", source_lang="en"
    )
    assert out.id == "0"
    assert out.target_lang == "fr"


async def test_use_memory_false_always_translates() -> None:
    svc = _service()
    seg = Segment(id="s0", text="Repeated line.")
    req = TranslationRequest(
        book_id="bk", target_lang="fr", segments=(seg,), source_lang="en", use_memory=False
    )
    await svc.translate(req)
    second = await svc.translate(req)
    assert second.cost.cache_hits == 0
    assert second.cost.provider_calls == 1


async def test_fuzzy_suggestion_surfaced() -> None:
    mem = TranslationMemory(fuzzy_threshold=0.8)
    svc = TranslationService(FakeTranslationProvider(), memory=mem)
    seg = Segment(id="s0", text="The cat sat on the mat.")
    await svc.translate(
        TranslationRequest(book_id="bk", target_lang="fr", segments=(seg,), source_lang="en")
    )
    # A near-identical source has no exact hit but a fuzzy suggestion.
    suggestion = svc.fuzzy_suggestion(
        "The cat sat on the mat!",
        source_lang="en",
        target_lang="fr",
        kind=ContentKind.PAGE_TEXT,
    )
    assert suggestion is not None
    text, ratio = suggestion
    assert ratio >= 0.8 and text


async def test_fuzzy_suggestion_none_when_no_match() -> None:
    svc = TranslationService(FakeTranslationProvider(), memory=TranslationMemory())
    assert (
        svc.fuzzy_suggestion(
            "nothing stored", source_lang="en", target_lang="fr", kind=ContentKind.PAGE_TEXT
        )
        is None
    )


async def test_narration_kind_routed() -> None:
    svc = _service()
    seg = Segment(id="s0", text="She stood by the window.", kind=ContentKind.NARRATION)
    res = await svc.translate(
        TranslationRequest(book_id="bk", target_lang="es", segments=(seg,), source_lang="en")
    )
    assert res.segments[0].translated_text  # produced something
