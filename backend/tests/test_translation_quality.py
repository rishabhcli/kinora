"""Unit tests for quality estimation + back-translation scoring."""

from __future__ import annotations

from app.translation.glossary import Glossary, GlossaryEntry
from app.translation.quality import estimate_quality, length_plausibility


def test_good_translation_scores_high() -> None:
    report = estimate_quality(
        source="The cat sat on the mat.",
        translated="Le chat etait assis sur le tapis.",
        source_lang="en",
        target_lang="fr",
    )
    assert report.score >= 0.8
    assert report.passed
    assert report.markup_ok


def test_markup_break_caps_score_low() -> None:
    report = estimate_quality(
        source="Hello {name}",
        translated="Bonjour",  # dropped placeholder
        source_lang="en",
        target_lang="fr",
    )
    assert not report.markup_ok
    assert report.score < 0.6
    assert not report.passed


def test_empty_translation_penalized() -> None:
    report = estimate_quality(
        source="Hello", translated="", source_lang="en", target_lang="fr"
    )
    assert report.score < 0.5
    assert any("empty" in w for w in report.warnings)


def test_identical_when_languages_differ_penalized() -> None:
    report = estimate_quality(
        source="Hello world",
        translated="Hello world",
        source_lang="en",
        target_lang="fr",
    )
    assert any("identical" in w for w in report.warnings)


def test_glossary_violation_lowers_score() -> None:
    g = Glossary([GlossaryEntry(source="Elsa", do_not_translate=True)])
    report = estimate_quality(
        source="Elsa smiled",
        translated="Elsi a souri",  # DNT term mangled
        source_lang="en",
        target_lang="fr",
        glossary=g,
    )
    assert not report.glossary_ok


def test_back_translation_high_agreement_boosts() -> None:
    high = estimate_quality(
        source="The dog runs fast",
        translated="Le chien court vite",
        source_lang="en",
        target_lang="fr",
        back_translation="The dog runs fast",
    )
    low = estimate_quality(
        source="The dog runs fast",
        translated="Le chien court vite",
        source_lang="en",
        target_lang="fr",
        back_translation="completely unrelated nonsense here",
    )
    assert high.score > low.score
    assert high.back_translation_ratio is not None


def test_length_plausibility_band() -> None:
    ok, penalty = length_plausibility("hello there", "bonjour", "fr")
    assert ok and penalty == 0.0
    bad, penalty2 = length_plausibility("hello", "x" * 200, "fr")
    assert not bad and penalty2 > 0.0
    empty_ok, empty_pen = length_plausibility("hello", "", "fr")
    assert not empty_ok and empty_pen == 1.0
