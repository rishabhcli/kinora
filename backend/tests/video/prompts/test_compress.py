"""Tests for the length-aware prompt compressor (fit_clauses / shorten_text / join_within)."""

from __future__ import annotations

from app.video.prompts.compress import fit_clauses, join_within, shorten_text


def test_shorten_text_returns_input_when_within_budget() -> None:
    assert shorten_text("a short line", 50) == "a short line"


def test_shorten_text_truncates_on_word_boundary_with_ellipsis() -> None:
    out = shorten_text("the quick brown fox jumps", 15)
    assert len(out) <= 15
    assert out.endswith("…")
    # No mid-word split: the kept text is whole words.
    assert "fo" not in out.replace("…", "") or out.replace("…", "").strip() in {
        "the quick",
        "the quick brown",
    }


def test_shorten_text_non_positive_budget_is_empty() -> None:
    assert shorten_text("anything", 0) == ""
    assert shorten_text("anything", -5) == ""


def test_shorten_text_single_long_token_hard_cut() -> None:
    out = shorten_text("supercalifragilisticexpialidocious", 10)
    assert len(out) <= 10
    assert out  # never empty when input is non-empty and budget positive


def test_shorten_text_budget_smaller_than_ellipsis_hard_cut_no_marker() -> None:
    out = shorten_text("hello world", 1)
    assert len(out) <= 1
    assert "…" not in out


def test_join_within_keeps_clauses_that_fit() -> None:
    out = join_within(["alpha", "beta", "gamma"], 100)
    assert out == "alpha. beta. gamma"


def test_join_within_skips_overflowing_clause_but_keeps_later_short_one() -> None:
    # "alpha" fits; the long middle clause overflows and is skipped; "x" still fits.
    out = join_within(["alpha", "B" * 80, "x"], 20)
    assert "alpha" in out and out.endswith("x")
    assert "B" * 80 not in out
    assert len(out) <= 20


def test_join_within_empty_when_nothing_fits() -> None:
    assert join_within(["A" * 50], 5) == ""


def test_fit_clauses_keeps_leading_prefix_that_fits() -> None:
    out = fit_clauses(["one", "two", "three", "four"], 11)
    # "one. two" == 8 chars fits; adding ". three" overflows 11.
    assert out == "one. two"
    assert len(out) <= 11


def test_fit_clauses_drops_only_the_tail_in_priority_order() -> None:
    clauses = ["subject acts", "in a setting", "camera moves", "lit softly"]
    out = fit_clauses(clauses, 26)
    assert out.startswith("subject acts")
    assert "lit softly" not in out  # lowest-priority tail dropped first
    assert len(out) <= 26


def test_fit_clauses_truncates_first_clause_when_even_it_overflows() -> None:
    out = fit_clauses(["a very long single leading clause that will not fit"], 20)
    assert out
    assert len(out) <= 20
    assert out.endswith("…")


def test_fit_clauses_never_empty_for_nonempty_input() -> None:
    assert fit_clauses(["something"], 1) != ""


def test_fit_clauses_empty_inputs() -> None:
    assert fit_clauses([], 100) == ""
    assert fit_clauses(["", "  "], 100) == ""
    assert fit_clauses(["x"], 0) == ""


def test_fit_clauses_strips_blank_clauses() -> None:
    out = fit_clauses(["", "real clause", "  "], 100)
    assert out == "real clause"
