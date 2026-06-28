"""Unit tests for the search analyzer: tokenize, fold, stem, synonyms, edit distance.

Pure + offline (no DB, no network). These pin the *consistency* property the
search engine relies on: a token analyzed at index time and at query time must
collapse to the same term.
"""

from __future__ import annotations

import pytest

from app.search.analyzer import (
    Analyzer,
    auto_fuzziness,
    damerau_levenshtein,
    default_analyzer,
    fold,
    stem,
    tokenize,
    within_distance,
)


def test_fold_lowercases_and_strips_accents() -> None:
    assert fold("Café RÉSUMÉ") == "cafe resume"


def test_tokenize_splits_words_and_drops_apostrophes() -> None:
    assert tokenize("It's a Snow-Queen!") == ["its", "a", "snow", "queen"]


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("running", "run"),
        ("ran", "run"),
        ("runs", "run"),
        ("happily", "happili"),  # consistent, not perfect
        ("children", "child"),
        ("women", "woman"),
    ],
)
def test_stem_conflates_morphological_variants(word: str, expected: str) -> None:
    assert stem(word) == expected


def test_stem_is_consistent_for_index_and_query() -> None:
    # The property that matters: variants share a stem.
    assert stem("running") == stem("ran") == stem("runs")


def test_analyzer_drops_stopwords() -> None:
    a = default_analyzer()
    terms = a.analyze("the snow and the ice")
    assert "the" not in terms
    assert "and" not in terms
    assert "snow" in terms
    assert "ice" in terms


def test_analyzer_expands_synonyms_to_canonical_head() -> None:
    a = default_analyzer()
    # "movie" and "film" both map to the head "film".
    assert a.normalize_token("movie") == a.normalize_token("film")
    assert a.normalize_token("novel") == a.normalize_token("book")


def test_analyzer_can_disable_stemming_and_synonyms() -> None:
    a = Analyzer(use_stemming=False, use_synonyms=False)
    assert a.normalize_token("running") == "running"
    assert a.normalize_token("movie") == "movie"


def test_analyze_positions_keeps_token_order() -> None:
    a = default_analyzer()
    positions = a.analyze_positions("snow queen rises")
    assert [t.position for t in positions] == [0, 1, 2]
    assert [t.surface for t in positions] == ["snow", "queen", "rises"]


def test_analyze_phrase_keeps_stopwords_for_positional_match() -> None:
    a = default_analyzer()
    # "to be" must remain findable as an ordered phrase.
    assert a.analyze_phrase("to be") == ["to", "be"]


@pytest.mark.parametrize(
    ("a", "b", "dist"),
    [
        ("kitten", "sitting", 3),
        ("frost", "frrost", 1),  # insertion
        ("character", "characrer", 1),  # substitution
        ("ab", "ba", 1),  # transposition
        ("same", "same", 0),
    ],
)
def test_damerau_levenshtein(a: str, b: str, dist: int) -> None:
    assert damerau_levenshtein(a, b) == dist


def test_damerau_levenshtein_early_exits_over_max() -> None:
    # Words far apart should exit at max+1 rather than compute the full distance.
    assert damerau_levenshtein("abcdef", "zzzzzz", max_distance=2) == 3


def test_within_distance() -> None:
    assert within_distance("frost", "frrost", 1)
    assert not within_distance("cat", "dog", 1)


@pytest.mark.parametrize(
    ("term", "budget"),
    [("cat", 0), ("frost", 1), ("character", 2)],
)
def test_auto_fuzziness_scales_with_length(term: str, budget: int) -> None:
    assert auto_fuzziness(term) == budget
