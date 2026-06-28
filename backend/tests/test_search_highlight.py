"""Unit tests for snippet extraction + term highlighting."""

from __future__ import annotations

from app.search.highlight import analyze_query_terms, highlight


def test_highlight_marks_matching_terms() -> None:
    terms = analyze_query_terms("snow queen")
    snip = highlight("The Snow Queen rules the north.", terms)
    assert "<mark>Snow</mark>" in snip.text
    assert "<mark>Queen</mark>" in snip.text


def test_highlight_marks_stemmed_variants() -> None:
    # Query "running" highlights "runs" because they stem together.
    terms = analyze_query_terms("running")
    snip = highlight("She runs to the castle.", terms)
    assert "<mark>runs</mark>" in snip.text


def test_highlight_escapes_html() -> None:
    terms = analyze_query_terms("dragon")
    snip = highlight("a <script>alert(1)</script> dragon tag", terms)
    # The raw tag is HTML-escaped (XSS-safe to render); the only literal '<' is
    # the highlight tag we add.
    assert "<script>" not in snip.text
    assert "&lt;script&gt;" in snip.text
    assert "<mark>dragon</mark>" in snip.text  # the matched word is marked


def test_highlight_picks_window_around_match() -> None:
    body = ("filler " * 60) + "the snow queen appears" + (" filler" * 60)
    terms = analyze_query_terms("snow queen")
    snip = highlight(body, terms, max_chars=80)
    assert "<mark>snow</mark>" in snip.text
    assert snip.truncated
    assert snip.text.startswith("…") or "snow" in snip.text[:90]


def test_highlight_no_match_returns_head() -> None:
    snip = highlight("nothing relevant here at all", analyze_query_terms("dragon"))
    assert "<mark>" not in snip.text
    assert snip.text.startswith("nothing")


def test_highlight_empty_text() -> None:
    snip = highlight("", analyze_query_terms("x"))
    assert snip.text == ""
    assert not snip.truncated
