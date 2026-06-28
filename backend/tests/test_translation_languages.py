"""Unit tests for the language registry (BCP-47 normalization + RTL)."""

from __future__ import annotations

import pytest

from app.translation.errors import UnknownLanguageError
from app.translation.languages import (
    TextDirection,
    canonical_tag,
    get_language,
    is_known,
    is_rtl,
    same_language,
    supported_languages,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en-US"),
        ("en_us", "en-US"),
        ("zh", "zh-Hans"),
        ("zh-cn", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh-tw", "zh-Hant"),
        ("zh-Hant-TW", "zh-Hant"),
        ("pt-br", "pt-BR"),
        ("pt-PT", "pt"),
        ("iw", "he"),  # legacy Hebrew alias
        ("fr-ca", "fr-CA"),
    ],
)
def test_canonical_tag_normalizes(raw: str, expected: str) -> None:
    assert canonical_tag(raw) == expected


def test_canonical_tag_rejects_empty() -> None:
    with pytest.raises(UnknownLanguageError):
        canonical_tag("")
    with pytest.raises(UnknownLanguageError):
        canonical_tag("   ")


def test_get_language_falls_back_to_primary() -> None:
    # fr-CA isn't in the registry, but fr is — resolve to fr.
    assert get_language("fr-CA").tag == "fr"
    assert get_language("en-GB").tag == "en"


def test_get_language_unknown_raises() -> None:
    with pytest.raises(UnknownLanguageError):
        get_language("xx-Qaaa")


@pytest.mark.parametrize("tag", ["ar", "he", "fa", "ur"])
def test_rtl_languages(tag: str) -> None:
    assert is_rtl(tag) is True
    assert get_language(tag).direction is TextDirection.RTL


@pytest.mark.parametrize("tag", ["en", "fr", "ru", "ja", "zh-Hans"])
def test_ltr_languages(tag: str) -> None:
    assert is_rtl(tag) is False
    assert get_language(tag).direction is TextDirection.LTR


def test_is_known() -> None:
    assert is_known("fr")
    assert is_known("zh-cn")
    assert not is_known("klingon")


def test_same_language() -> None:
    assert same_language("en", "en-US")
    assert same_language("zh", "zh-Hans")
    assert not same_language("en", "fr")


def test_supported_languages_sorted_and_nonempty() -> None:
    langs = supported_languages()
    assert len(langs) >= 20
    names = [lang.name for lang in langs]
    assert names == sorted(names)


def test_primary_subtag() -> None:
    assert get_language("pt-BR").primary_subtag == "pt"
    assert get_language("en").primary_subtag == "en"


def test_endonyms_present() -> None:
    assert get_language("ja").endonym == "日本語"
    assert get_language("ar").endonym == "العربية"
