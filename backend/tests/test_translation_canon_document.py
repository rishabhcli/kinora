"""Tests for canon-glossary integration and document-level translation."""

from __future__ import annotations

from collections.abc import Sequence

from app.translation.canon import (
    CanonName,
    build_book_glossary,
    glossary_from_canon_names,
    merge_glossaries,
)
from app.translation.document import DocumentTranslator
from app.translation.glossary import Glossary, GlossaryEntry
from app.translation.memory_store import TranslationMemory
from app.translation.provider import FakeTranslationProvider
from app.translation.service import TranslationService

# NOTE: ``asyncio_mode = "auto"`` (pyproject) runs the async tests below without
# an explicit mark; this module mixes sync + async tests, so no module-level
# ``pytestmark`` (which would warn on the sync ones).


# -- canon integration ------------------------------------------------------- #


def test_glossary_from_canon_names_locks_names_and_aliases() -> None:
    names = [
        CanonName(entity_key="char_elsa", name="Elsa", aliases=("the Snow Queen",)),
        CanonName(entity_key="char_anna", name="Anna"),
    ]
    g = glossary_from_canon_names(names)
    sources = sorted(e.source for e in g.entries)
    assert sources == ["Anna", "Elsa", "the Snow Queen"]
    assert all(e.do_not_translate for e in g.entries)


def test_glossary_from_canon_dedupes_surface_forms() -> None:
    names = [CanonName(entity_key="x", name="Elsa", aliases=("Elsa", " Elsa "))]
    g = glossary_from_canon_names(names)
    assert len([e for e in g.entries if e.source == "Elsa"]) == 1


def test_glossary_from_canon_with_extra_terms() -> None:
    names = [CanonName(entity_key="x", name="Elsa")]
    extra = [GlossaryEntry(source="kingdom", targets={"fr": "royaume"})]
    g = glossary_from_canon_names(names, extra=extra)
    assert any(e.source == "kingdom" for e in g.entries)


def test_merge_glossaries_max_version_and_upsert() -> None:
    a = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)], version=2)
    b = Glossary([GlossaryEntry(source="Anna", do_not_translate=True)], version=5)
    merged = merge_glossaries(a, b)
    assert merged.version == 5
    assert {e.source for e in merged.entries} == {"Elsa", "Anna"}


async def test_build_book_glossary_from_source() -> None:
    class FakeSource:
        async def character_names(self, book_id: str) -> Sequence[CanonName]:
            assert book_id == "bk1"
            return [CanonName(entity_key="c", name="Elsa")]

    g = await build_book_glossary(FakeSource(), "bk1")
    assert any(e.source == "Elsa" for e in g.entries)


# -- document translation ---------------------------------------------------- #


def _doc_translator(glossary: Glossary | None = None) -> DocumentTranslator:
    svc = TranslationService(
        FakeTranslationProvider(), glossary=glossary, memory=TranslationMemory()
    )
    return DocumentTranslator(svc)


async def test_translate_page_preserves_paragraphs() -> None:
    dt = _doc_translator()
    page = "First paragraph. It has two sentences.\n\nSecond paragraph here."
    doc = await dt.translate_page(page, book_id="bk", target_lang="fr", source_lang="en")
    # The blank-line paragraph break is preserved.
    assert "\n\n" in doc.text
    # Two paragraphs → first has two sentences stitched with a space.
    assert doc.text.count("\n\n") == 1
    assert len(doc.segments) == 3  # 2 + 1 sentences


async def test_translate_page_keeps_markup_per_sentence() -> None:
    dt = _doc_translator()
    page = "See <b>{name}</b> now. Then leave."
    doc = await dt.translate_page(page, book_id="bk", target_lang="fr", source_lang="en")
    assert "<b>{name}</b>" in doc.text


async def test_translate_narration_speakable_join() -> None:
    dt = _doc_translator()
    script = "She stood by the window. The snow fell softly."
    doc = await dt.translate_narration(script, book_id="bk", target_lang="es", source_lang="en")
    # Narration stitches sentences with single spaces, no paragraph breaks.
    assert "\n\n" not in doc.text
    assert len(doc.segments) == 2


async def test_translate_page_rtl_flag() -> None:
    dt = _doc_translator()
    doc = await dt.translate_page("Hello world.", book_id="bk", target_lang="ar", source_lang="en")
    assert doc.rtl is True


async def test_translate_page_empty() -> None:
    dt = _doc_translator()
    doc = await dt.translate_page("   ", book_id="bk", target_lang="fr", source_lang="en")
    assert doc.text == ""
    assert doc.segments == ()


async def test_translate_entity_description_with_dnt() -> None:
    g = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)])
    dt = _doc_translator(g)
    doc = await dt.translate_entity_description(
        "Elsa is a young woman with a platinum braid.",
        book_id="bk",
        target_lang="fr",
        source_lang="en",
        entity_key="char_elsa",
    )
    assert "Elsa" in doc.text  # name locked
    assert doc.target_lang == "fr"


async def test_document_review_propagates() -> None:
    svc = TranslationService(
        FakeTranslationProvider(corrupt_markup=True), memory=TranslationMemory()
    )
    dt = DocumentTranslator(svc)
    doc = await dt.translate_page(
        "Keep {a} and {b} here.", book_id="bk", target_lang="fr", source_lang="en"
    )
    assert doc.needs_review
