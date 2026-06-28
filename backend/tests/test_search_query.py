"""Unit tests for the query parser: phrases, boolean, filters, facets, ranges."""

from __future__ import annotations

from app.search.query import (
    Occur,
    RangeFilter,
    RangeOp,
    parse_query,
    range_matches,
)


def test_plain_terms_are_should() -> None:
    pq = parse_query("snow queen")
    assert [t.text for t in pq.terms] == ["snow", "queen"]
    assert all(t.occur is Occur.SHOULD for t in pq.terms)
    assert pq.has_text


def test_quoted_phrase() -> None:
    pq = parse_query('"snow queen" rises')
    assert len(pq.phrases) == 1
    assert pq.phrases[0].text == "snow queen"
    assert [t.text for t in pq.terms] == ["rises"]


def test_plus_minus_occurrence() -> None:
    pq = parse_query("+frost -summer castle")
    by_text = {t.text: t.occur for t in pq.terms}
    assert by_text["frost"] is Occur.MUST
    assert by_text["summer"] is Occur.MUST_NOT
    assert by_text["castle"] is Occur.SHOULD


def test_boolean_operators_bind_next_clause() -> None:
    pq = parse_query("frost AND castle OR ice NOT summer")
    by_text = {t.text: t.occur for t in pq.terms}
    assert by_text["castle"] is Occur.MUST
    assert by_text["ice"] is Occur.SHOULD
    assert by_text["summer"] is Occur.MUST_NOT


def test_keyword_field_becomes_filter() -> None:
    pq = parse_query("frost kind:beat")
    assert len(pq.filters) == 1
    assert pq.filters[0].field == "kind"
    assert pq.filters[0].value == "beat"
    assert [t.text for t in pq.terms] == ["frost"]


def test_negated_keyword_filter() -> None:
    pq = parse_query("-kind:shot frost")
    assert pq.filters[0].field == "kind"
    assert pq.filters[0].negate is True


def test_numeric_range_op_filter() -> None:
    pq = parse_query("page:>=10")
    assert len(pq.ranges) == 1
    assert pq.ranges[0].field == "page"
    assert pq.ranges[0].op is RangeOp.GTE
    assert pq.ranges[0].value == 10.0


def test_to_range_filter() -> None:
    pq = parse_query("page:[3 TO 9]")
    assert len(pq.ranges) == 1
    r = pq.ranges[0]
    assert r.lo == 3.0
    assert r.hi == 9.0


def test_text_field_scope() -> None:
    pq = parse_query("title:queen")
    assert len(pq.terms) == 1
    assert pq.terms[0].field == "title"


def test_unknown_field_falls_back_to_text() -> None:
    pq = parse_query("foo:bar")
    # Not silently dropped: kept as a literal text term.
    assert any(t.text == "foo:bar" for t in pq.terms)


def test_empty_query() -> None:
    pq = parse_query("   ")
    assert pq.is_empty
    assert not pq.has_text


def test_free_text_excludes_must_not() -> None:
    pq = parse_query("frost -summer")
    assert "frost" in pq.free_text
    assert "summer" not in pq.free_text


def test_range_matches_op() -> None:
    assert range_matches(RangeFilter(field="page", op=RangeOp.GTE, value=10), 12)
    assert not range_matches(RangeFilter(field="page", op=RangeOp.GTE, value=10), 5)


def test_range_matches_to_bounds() -> None:
    rf = RangeFilter(field="page", lo=3.0, hi=9.0)
    assert range_matches(rf, 5)
    assert not range_matches(rf, 2)
    assert not range_matches(rf, 10)


def test_range_matches_none_value_is_false() -> None:
    assert not range_matches(RangeFilter(field="page", op=RangeOp.GTE, value=10), None)
