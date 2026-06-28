"""Unit tests for the translation memory (exact + fuzzy) and edit distance."""

from __future__ import annotations

from app.translation.memory_store import (
    MemoryEntry,
    TranslationMemory,
    levenshtein,
    similarity_ratio,
)
from app.translation.types import ContentKind


def _entry(src: str, tgt: str, *, lang: str = "fr", gver: int = 0) -> MemoryEntry:
    return MemoryEntry(
        source_text=src,
        translated_text=tgt,
        source_lang="en",
        target_lang=lang,
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=gver,
    )


def test_levenshtein_basics() -> None:
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "abc") == 0


def test_similarity_ratio() -> None:
    assert similarity_ratio("abc", "abc") == 1.0
    assert 0.0 <= similarity_ratio("abc", "xyz") < 0.5
    assert similarity_ratio("", "") == 1.0


def test_exact_hit_and_miss() -> None:
    mem = TranslationMemory()
    mem.put(_entry("Hello world", "Bonjour le monde"))
    hit = mem.get_exact(
        source_text="Hello world",
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert hit is not None
    assert hit.translated_text == "Bonjour le monde"
    miss = mem.get_exact(
        source_text="Goodbye",
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert miss is None


def test_glossary_version_changes_key() -> None:
    mem = TranslationMemory()
    mem.put(_entry("Hello", "Bonjour", gver=0))
    # A different glossary version is a cache miss (renamed term invalidates).
    assert (
        mem.get_exact(
            source_text="Hello",
            source_lang="en",
            target_lang="fr",
            content_kind=ContentKind.PAGE_TEXT,
            glossary_version=1,
        )
        is None
    )


def test_fuzzy_match_above_threshold() -> None:
    mem = TranslationMemory(fuzzy_threshold=0.8)
    mem.put(_entry("The cat sat on the mat.", "Le chat etait sur le tapis."))
    fuzzy = mem.get_fuzzy(
        source_text="The cat sat on the mat!",  # one char differs
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert fuzzy is not None
    assert fuzzy.ratio >= 0.8


def test_fuzzy_below_threshold_returns_none() -> None:
    mem = TranslationMemory(fuzzy_threshold=0.95)
    mem.put(_entry("The cat sat on the mat.", "x"))
    assert (
        mem.get_fuzzy(
            source_text="Completely different sentence entirely.",
            source_lang="en",
            target_lang="fr",
            content_kind=ContentKind.PAGE_TEXT,
            glossary_version=0,
        )
        is None
    )


def test_put_is_idempotent_on_source() -> None:
    mem = TranslationMemory()
    mem.put(_entry("Hello", "v1"))
    mem.put(_entry("Hello", "v2"))
    assert len(mem) == 1
    hit = mem.get_exact(
        source_text="Hello",
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert hit is not None and hit.translated_text == "v2"


def test_clear() -> None:
    mem = TranslationMemory()
    mem.put(_entry("Hello", "Bonjour"))
    mem.clear()
    assert len(mem) == 0
